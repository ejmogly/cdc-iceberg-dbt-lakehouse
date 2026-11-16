"""F 화면의 계산 서버(docs/ai/F-screen.md 2절 「구조」). 파이썬 표준 라이브러리 HTTP 서버와 JSON API입니다.

사용: .venv/bin/python -m ai.screen_server [--db data/generated/screen.duckdb] [--port 8765]

API(127.0.0.1에서만 받고, Host 머리도 127.0.0.1이나 localhost만)
  GET  /api/status   10번 칸. 내보내기 검사와 지문 대조. ok가 거짓이면 아래 API는 503과 실패 항목만 돌려줍니다
  GET  /api/symbols  종목과 데이터 기간
  POST /api/pnl      {"symbol", "asof", "trades": [{"date", "side": "BUY"|"SELL", "qty", "price"?, "fx_rate"?}]}
                     요약(2번 칸), 가격과 내 매매와 큰 움직임(3번), 누적 손익과 SPY 선(4번), 원화 수익 나누기(5번),
                     매도별 손익(6번), 경고(코드, 줄 번호, 문구)

- 서버는 화면 데이터 파일만 읽고 VM을 켜지 않습니다. 시작할 때 연결 하나로 검사와 지문을 대조하고 시세와 환율을
  메모리로 읽습니다. 기사와 일정은 같은 연결로 그때그때 고릅니다.
- 손익은 ai/pnl.py compute(제품 SQL)이고 묻고 답하기의 도구와 같은 계산입니다. 체결가와 환율을 비우면 손익 SQL이
  그날 종가와 매매기준율을 씁니다(1번 칸).
- 손익 SQL까지 간 줄이 하나도 없으면(모든 줄이 데이터 기간 밖이거나 소수 수량) summary는 null입니다. SQL까지 갔지만
  장부에 들어간 매수가 없으면(수량 0, 기준일 뒤, 초과 매도뿐) summary는 SQL의 값(0)이고 booked는 false입니다.
- 보유 기간은 장부에 들어간 첫 매수의 매매일부터 기준일까지입니다. 장부에 들어간 매수가 없으면 null입니다.
- 큰 움직임은 E02 정의입니다: 수정한 종가의 하루 단순 수익률이, 그날을 뺀 앞 60거래일 수익률의 표본 표준편차의
  2배를 넘는 날. 앞 수익률 60개가 없는 날(데이터 첫 61거래일)은 판정하지 않고 judged_from부터 판정합니다.
"""
import argparse
import json
import statistics
import sys
import threading
import time
import traceback
from decimal import ROUND_HALF_UP, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ai import screen_data
from ai.pnl import compute, iso_date, number
from ai.tools import real_data

ROOT = Path(__file__).resolve().parents[1]
WARNINGS = json.loads((ROOT / "eval" / "f_screen" / "warnings.json").read_text(encoding="utf-8"))
STATIC = (ROOT / "screen").resolve()
MAX_TRADES = 200
SIX = Decimal("0.000001")


class BadRequest(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _round(x, places):
    """손익 SQL(pnl_position)처럼 소수 여섯째 자리로 먼저 맞춘 뒤 0.5는 0에서 먼 쪽으로."""
    if x is None:
        return None
    q = Decimal(str(x)).quantize(SIX, rounding=ROUND_HALF_UP).quantize(Decimal(1).scaleb(-places),
                                                                      rounding=ROUND_HALF_UP)
    return int(q) if places == 0 else float(q)


def won(x):
    return _round(x, 0)


def _num(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _last_on_or_before(days, d):
    lo = [x for x in days if x <= d]
    return lo[-1] if lo else None


def big_moves(rows, start, end):
    """보유 기간 start ~ end의 큰 움직임 날(E02 정의, 수정한 종가)과 판정을 시작할 수 있는 첫날."""
    days = sorted(rows)
    px = [Decimal(str(rows[d]["adj_close"] if rows[d].get("adj_close") is not None else rows[d]["close"])) for d in days]
    rets = [0.0] + [float(px[i] / px[i - 1] - 1) for i in range(1, len(px))]
    judged = days[61] if len(days) > 61 else None
    dates = [days[i] for i in range(61, len(days))
             if start <= days[i] <= end and abs(rets[i]) > 2 * statistics.stdev(rets[i - 60:i])]
    return {"dates": dates, "judged_from": judged, "not_judged_in_holding": start < judged if judged else True}


class App:
    def __init__(self, db):
        self.db = Path(db)
        self.data = real_data(self.db) if self.db.exists() else None
        self.status = self.data.status if self.data else screen_data.MISSING
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")

    def close(self):
        if self.data:
            self.data.close()

    # ---------- API ----------

    def api_status(self):
        return {**self.status, "server_started": self.started, "db": self.db.name}

    def api_symbols(self):
        d = self.data
        out = []
        for s, v in sorted(d.symbols.items(), key=lambda kv: (kv[1]["market"] != "KRX", kv[0])):
            rows = d.rows[s]
            out.append({"symbol": s, "name": v["name"], "market": v["market"], "currency": v["currency"],
                        "first": rows[0], "last": rows[-1]})
        return {"symbols": out, "calendar": [d.calendar[0], d.calendar[-1]], "bench": d.bench}

    def _trades(self, body):
        trades = body.get("trades")
        if not isinstance(trades, list) or not trades:
            raise BadRequest("NO_TRADES", "매매 줄이 없습니다")
        if len(trades) > MAX_TRADES:
            raise BadRequest("TOO_MANY_TRADES", f"매매 줄은 {MAX_TRADES}개까지입니다")
        out = []
        for k, t in enumerate(trades):
            if not isinstance(t, dict):
                raise BadRequest("BAD_TRADE", f"{k}번 줄의 모양이 틀렸습니다")
            if not iso_date(t.get("date")):
                raise BadRequest("BAD_DATE", f"{k}번 줄의 날짜는 YYYY-MM-DD로 주세요")
            side = str(t.get("side", "")).upper()
            if side not in ("BUY", "SELL"):
                raise BadRequest("BAD_TRADE", f"{k}번 줄의 매매는 BUY나 SELL입니다")
            row = {"d": t["date"], "side": side}
            for key, name, label in (("qty", "q", "수량"), ("price", "p", "체결가"), ("fx_rate", "fx", "환율")):
                v = t.get(key)
                if v is None or v == "":
                    if key == "qty":
                        raise BadRequest("BAD_TRADE", f"{k}번 줄의 수량이 없습니다")
                    continue
                try:
                    v = number(v, {"q": "qty", "p": "price", "fx": "fx"}[name])
                except ValueError as e:
                    raise BadRequest("BAD_TRADE", f"{k}번 줄의 {label}: {e}") from None
                row[name] = (int(v) if v == v.to_integral_value() else v) if name == "q" else v
            out.append(row)
        return out

    def _message(self, w, symbol, trades):
        d = self.data
        rows = d.rows[symbol]
        t = trades[w["trade_index"]]
        fill = {}
        if w["code"] == "NON_TRADING_DAY":
            fill["used_date"] = _last_on_or_before(rows, t["d"])
        elif w["code"] == "OUT_OF_DATA_RANGE":
            fill = {"first": rows[0], "last": rows[-1]}
        elif w["code"] == "FX_CARRIED":
            fill["fx_date"] = _last_on_or_before(d.fx_days, _last_on_or_before(d.calendar, t["d"]))
        return {**w, "trade_date": t["d"], "message": WARNINGS[w["code"]].format(**fill), **fill}

    def api_pnl(self, body):
        d = self.data
        symbol = body.get("symbol")
        if not isinstance(symbol, str) or symbol not in d.symbols:
            raise BadRequest("UNKNOWN_SYMBOL", f"없는 종목입니다: {symbol}")
        asof = body.get("asof")
        if not iso_date(asof):
            raise BadRequest("BAD_DATE", "기준일은 YYYY-MM-DD로 주세요")
        if not d.calendar[0] <= asof <= d.calendar[-1]:
            raise BadRequest("OUT_OF_DATA_RANGE", f"기준일은 데이터 기간({d.calendar[0]} ~ {d.calendar[-1]}) 안이어야 합니다")
        trades = self._trades(body)
        t0 = time.perf_counter()
        with d.lock:
            summary, sells, warnings, kept = compute(d.engine(), d.world, symbol, trades, asof, d.bench)
            daily, steps = d.engine().series() if kept else ([], [])
        rows = d.symbols[symbol]["rows"]
        if summary:
            summary = {k: _num(v) for k, v in summary.items() if not k.startswith("gap_")}
            summary["total_minus_bench_krw"] = summary["total_krw"] - summary["bench_total_krw"]
            summary["naive_minus_total_krw"] = summary["naive_krw"] - summary["total_krw"]
        buys = [kept[seq] for seq, _, side, *_ in steps if side == "BUY"]
        holding = [trades[buys[0]]["d"], asof] if buys else None
        return {
            "symbol": symbol, "name": d.symbols[symbol]["name"], "currency": d.symbols[symbol]["currency"],
            "asof": asof, "asof_price_date": _last_on_or_before(d.rows[symbol], asof), "bench": d.bench,
            "summary": summary or None, "booked": bool(buys),
            "sells": [{k: _num(v) for k, v in s.items()} for s in sells],
            "warnings": [self._message(w, symbol, trades) for w in warnings],
            "steps": [{"trade_index": kept[seq], "date": cd.isoformat(), "side": side, "qty": q, "price": _num(p),
                       "fx": _num(fx), "excluded": bool(over), "qty_after": qa,
                       "avg_cost_after": _round(cost / qa, 4) if qa else None}
                      for seq, cd, side, q, p, fx, over, qa, cost in steps],
            "daily": [{"date": c.isoformat(), "pnl_krw": won(p), "peak_krw": won(pk), "bench_krw": won(b)}
                      for c, p, pk, b in daily],
            "prices": [[x, _num(rows[x]["close"])] for x in d.rows[symbol]],
            "big_moves": big_moves(rows, holding[0], holding[1]) if holding else None,
            "holding": holding,
            "seconds": round(time.perf_counter() - t0, 4),
        }


def make_handler(app, port_holder):
    class Handler(BaseHTTPRequestHandler):
        server_version = "screen/1"

        def log_message(self, format, *args):  # noqa: A002  (기본 접속 기록을 끔)
            pass

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            port = port_holder[0]
            if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return True
            self._send(403, {"error": "BAD_HOST", "message": "127.0.0.1이나 localhost로 여세요"})
            return False

        def _gate(self):
            if app.data is None or not app.status["ok"]:
                self._send(503, {"error": "PIPELINE_CHECK_FAILED", "failures": app.status["failures"]})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if path == "/api/status":
                return self._send(200, app.api_status())
            if path == "/api/symbols":
                return self._gate() and self._send(200, app.api_symbols())
            return self._static(path)

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path != "/api/pnl":
                return self._send(404, {"error": "NOT_FOUND"})
            if not self._gate():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:  # 길이 머리, JSON, UTF-8을 읽지 못함
                return self._send(400, {"error": "BAD_JSON", "message": "JSON을 읽지 못했습니다"})
            try:
                if not isinstance(body, dict):
                    raise BadRequest("BAD_JSON", "JSON 객체를 보내 주세요")
                return self._send(200, app.api_pnl(body))
            except BadRequest as e:
                return self._send(400, {"error": e.code, "message": e.message})
            except Exception as e:  # 그 밖의 오류도 연결을 끊지 않고 JSON으로 알립니다
                traceback.print_exc(file=sys.stderr)
                return self._send(500, {"error": "INTERNAL", "message": type(e).__name__})

        def _static(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if not f.is_relative_to(STATIC) or not f.is_file():
                return self._send(404, {"error": "NOT_FOUND"})
            types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
            return self._send(200, f.read_bytes(), types.get(f.suffix, "application/octet-stream"))

    return Handler


def serve(db, port=8765):
    """서버를 띄우고 (서버, 스레드)를 돌려줍니다(port=0이면 빈 포트). 끌 때는 stop(서버)."""
    app = App(db)
    holder = [port]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, holder))
    holder[0] = httpd.server_address[1]
    httpd.app = app
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return httpd, th


def stop(httpd):
    httpd.shutdown()
    httpd.server_close()
    httpd.app.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(screen_data.OUT))
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    httpd, th = serve(a.db, a.port)
    st = httpd.app.status
    print(f"화면 서버: http://127.0.0.1:{httpd.server_address[1]}/  데이터 {Path(a.db).name}, "
          f"파이프라인 검사 {'통과' if st['ok'] else '실패: ' + ', '.join(f['name'] for f in st['failures'])}")
    try:
        th.join()
    except KeyboardInterrupt:
        stop(httpd)
