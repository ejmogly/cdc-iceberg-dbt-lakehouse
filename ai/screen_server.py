"""F 화면의 계산 서버(docs/ai/F-screen.md 2절 「구조」). 파이썬 표준 라이브러리 HTTP 서버와 JSON API입니다.

사용: .venv/bin/python -m ai.screen_server [--db data/generated/screen.duckdb] [--port 8765]

API(127.0.0.1에서만 받고, Host 머리도 127.0.0.1이나 localhost만)
  GET  /api/status   10번 칸. 내보내기 검사와 지문 대조. ok가 거짓이면 아래 API는 503과 실패 항목만 돌려줍니다
  GET  /api/symbols  종목과 데이터 기간
  POST /api/pnl      {"symbol", "asof", "trades": [{"date", "side": "BUY"|"SELL", "qty", "price"?, "fx_rate"?}]}
                     요약(2번 칸), 가격과 내 매매와 큰 움직임(3번), 누적 손익과 SPY 선(4번), 원화 수익 나누기(5번),
                     매도별 손익(6번), 경고(코드, 줄 번호, 문구). 한 줄의 입력이 틀리면 400과 그 줄 번호(trade_index).
                     금액이 손익 SQL의 decimal 범위를 넘으면(약 100억 원) 400 AMOUNT_TOO_LARGE와 금액이 가장 큰 줄
  GET  /api/trades   화면에 넣어 둔 매매(종목마다). 저장 파일을 주지 않고 띄우면 {"enabled": false}
  PUT  /api/trades   {"symbol", "asof", "trades"} 한 종목의 매매를 저장(설계서 6절 4번: data/generated/ 아래 파일)

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
import os
import statistics
import sys
import threading
import time
import traceback
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb

from ai import screen_data
from ai.pnl import compute, iso_date, number
from ai.tools import real_data

SAFE = (Path(__file__).resolve().parents[1] / "data" / "generated").resolve()

ROOT = Path(__file__).resolve().parents[1]
WARNINGS = json.loads((ROOT / "eval" / "f_screen" / "warnings.json").read_text(encoding="utf-8"))
STATIC = (ROOT / "screen").resolve()
TRADES = SAFE / "f_screen" / "screen_trades.json"
MAX_TRADES = 200
TRADE_KEYS = ("date", "side", "qty", "price", "fx_rate")
SIX = Decimal("0.000001")


class BadRequest(Exception):
    def __init__(self, code, message, index=None):
        super().__init__(message)
        self.code, self.message, self.index = code, message, index

    def payload(self):
        out = {"error": self.code, "message": self.message}
        if self.index is not None:
            out["trade_index"] = self.index
        return out


class SavedFileError(Exception):
    """저장 파일이 깨졌거나 모양이 틀림. 덮어쓰지 않고 알립니다(사용자가 고치거나 지움)."""


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
    def __init__(self, db, trades=None):
        self.db = Path(db)
        self.data = real_data(self.db) if self.db.exists() else None
        self.status = self.data.status if self.data else screen_data.MISSING
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        # 넣은 매매는 실제 시세에서 나온 값(비운 칸을 채운 값)이 아니라 사용자가 친 글자 그대로지만, 실제 데이터와 같은
        # 자리(git 제외)에 둡니다. 그 밖의 자리를 주면 멈춥니다.
        self.trades_path = Path(trades).resolve() if trades else None
        if self.trades_path and not self.trades_path.is_relative_to(SAFE):
            raise ValueError(f"매매 저장 파일은 {SAFE} 아래여야 합니다: {self.trades_path}")
        self.trades_lock = threading.Lock()

    def close(self):
        if self.data:
            self.data.close()

    def warm(self):
        """서버를 띄운 뒤 첫 계산만 손익 SQL을 처음 준비하느라 2 ~ 3초 걸려(그 뒤는 0.1초 안), 띄울 때 한 번 미리 돌려
        둡니다. 결과는 버리고, 실패해도 서버는 그대로 뜹니다(그때는 첫 계산이 느릴 뿐)."""
        d = self.data
        if not d or not self.status.get("ok"):
            return None
        t0 = time.perf_counter()
        try:
            days = d.rows[d.bench]
            self.api_pnl({"symbol": d.bench, "asof": days[-1], "trades": [{"date": days[0], "side": "BUY", "qty": "1"}]})
        except Exception as e:  # noqa: BLE001 - 미리 돌리기는 실패해도 됨(첫 계산이 느릴 뿐)
            print(f"계산 미리 돌리기 실패(첫 계산이 느릴 수 있음): {type(e).__name__}: {e}")
            return None
        return round(time.perf_counter() - t0, 2)

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
            n = k + 1  # 문구의 줄 번호는 1부터, trade_index는 0부터
            if not isinstance(t, dict):
                raise BadRequest("BAD_TRADE", f"{n}번 줄의 모양이 틀렸습니다", k)
            if not iso_date(t.get("date")):
                raise BadRequest("BAD_DATE", f"{n}번 줄의 날짜는 YYYY-MM-DD로 주세요", k)
            side = str(t.get("side", "")).upper()
            if side not in ("BUY", "SELL"):
                raise BadRequest("BAD_TRADE", f"{n}번 줄의 매매는 BUY나 SELL입니다", k)
            row = {"d": t["date"], "side": side}
            for key, name, label in (("qty", "q", "수량"), ("price", "p", "체결가"), ("fx_rate", "fx", "환율")):
                v = t.get(key)
                if v is None or v == "":
                    if key == "qty":
                        raise BadRequest("BAD_TRADE", f"{n}번 줄의 수량이 없습니다", k)
                    continue
                try:
                    v = number(v, {"q": "qty", "p": "price", "fx": "fx"}[name])
                except ValueError as e:
                    raise BadRequest("BAD_TRADE", f"{n}번 줄의 {label}: {e}", k) from None
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
        try:
            with d.lock:
                summary, sells, warnings, kept = compute(d.engine(), d.world, symbol, trades, asof, d.bench)
                daily, steps = d.engine().series() if kept else ([], [])
        except duckdb.OutOfRangeException:
            # 손익 SQL의 decimal 곱셈이 넘침(매매 금액이나 기준일 평가액이 약 100억 원). 가장 큰 매매를 짚어 줍니다
            k = max(range(len(trades)), key=lambda i: self._amount(symbol, trades[i]))
            raise BadRequest("AMOUNT_TOO_LARGE",
                             f"금액이 계산할 수 있는 범위(매매 한 건이나 기준일 평가액이 약 100억 원)를 넘었습니다. "
                             f"금액이 가장 큰 {k + 1}번 줄에서 수량, 체결가, 환율의 단위를 확인하세요", k) from None
        rows = d.symbols[symbol]["rows"]
        if summary:
            summary = {k: _num(v) for k, v in summary.items() if not k.startswith("gap_")}
            summary["total_minus_bench_krw"] = summary["total_krw"] - summary["bench_total_krw"]
            summary["naive_minus_total_krw"] = summary["naive_krw"] - summary["total_krw"]
        buys = [kept[seq] for seq, _, side, *_ in steps if side == "BUY"]
        holding = [trades[buys[0]]["d"], asof] if buys else None
        # 연 환산이 없는 까닭(pnl_irr): 첫 매매일부터 기준일(달력에 맞춘 날)까지 30일 미만인지
        live = [cd for _, cd, _, _, _, _, over, *_ in steps if not over]
        end = _last_on_or_before(d.calendar, asof)
        irr_short = bool(live) and (date.fromisoformat(end) - min(live)).days < 30
        took = time.perf_counter() - t0
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
            "irr_short": irr_short,
            "seconds": round(took, 4),
            "ms": round(took * 1000),
        }


    # ---------- 넣은 매매 저장(설계서 6절 4번) ----------

    def _amount(self, symbol, t):
        """매매 금액(현지 통화, 수량 x 체결가나 그날 종가). 넘친 계산에서 짚을 줄을 고를 때만 씁니다."""
        rows = self.data.symbols[symbol]["rows"]
        if t.get("p") is not None:
            px = Decimal(str(t["p"]))
        else:
            day = _last_on_or_before(self.data.rows[symbol], t["d"])
            px = Decimal(str(rows[day]["close"])) if day else Decimal(0)
        return abs(Decimal(str(t["q"]))) * px

    def _saved(self):
        try:
            saved = json.loads(self.trades_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"symbols": {}, "last": None}
        except (ValueError, OSError) as e:
            raise SavedFileError(f"저장 파일을 읽지 못했습니다({self.trades_path.name}, {type(e).__name__}). "
                                 f"덮어쓰지 않았습니다. 파일을 고치거나 옮긴 뒤 서버를 다시 띄우세요") from None
        if not isinstance(saved, dict) or not isinstance(saved.get("symbols"), dict):
            raise SavedFileError(f"저장 파일의 모양이 틀렸습니다({self.trades_path.name}). "
                                 f"덮어쓰지 않았습니다. 파일을 고치거나 옮긴 뒤 서버를 다시 띄우세요")
        return saved

    def api_trades(self):
        if not self.trades_path:
            return {"enabled": False}
        with self.trades_lock:
            return {"enabled": True, **self._saved()}

    def api_save_trades(self, body):
        if not self.trades_path:
            raise BadRequest("SAVE_DISABLED", "이 서버는 매매를 저장하지 않습니다")
        symbol, asof, trades = body.get("symbol"), body.get("asof"), body.get("trades")
        if not isinstance(symbol, str) or symbol not in self.data.symbols:
            raise BadRequest("UNKNOWN_SYMBOL", f"없는 종목입니다: {symbol}")
        if not iso_date(asof):
            raise BadRequest("BAD_DATE", "기준일은 YYYY-MM-DD로 주세요")
        if not isinstance(trades, list) or len(trades) > MAX_TRADES:
            raise BadRequest("TOO_MANY_TRADES", f"매매 줄은 {MAX_TRADES}개까지입니다")
        clean = []
        for k, t in enumerate(trades):
            # 고치는 중인 줄(빈 칸, 아직 틀린 값)도 그대로 남깁니다. 칸 이름과 길이만 막습니다.
            if not isinstance(t, dict) or set(t) - set(TRADE_KEYS) or any(
                    not isinstance(v, (str, int, float)) or isinstance(v, bool) or len(str(v)) > 32 for v in t.values()):
                raise BadRequest("BAD_TRADE", f"{k + 1}번 줄의 모양이 틀렸습니다", k)
            clean.append({key: str(t[key]) for key in TRADE_KEYS if key in t})
        with self.trades_lock:
            saved = self._saved()
            saved["symbols"][symbol] = {"asof": asof, "trades": clean,
                                        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            saved["last"] = symbol
            self.trades_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.trades_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(saved, ensure_ascii=False, indent=1))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.trades_path)
        return {"ok": True, "saved_at": saved["symbols"][symbol]["saved_at"]}


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
            hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
            if self.headers.get("Host") not in hosts:
                self._send(403, {"error": "BAD_HOST", "message": "127.0.0.1이나 localhost로 여세요"})
                return False
            # 다른 사이트의 페이지가 이 서버로 보내는 요청(Origin이 다름)은 받지 않습니다
            origin = self.headers.get("Origin")
            if origin is not None and origin not in tuple(f"http://{h}" for h in hosts):
                self._send(403, {"error": "BAD_ORIGIN", "message": "이 화면에서만 부를 수 있습니다"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 1_000_000:
                raise BadRequest("BAD_LENGTH", "본문 길이가 틀렸습니다")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise BadRequest("BAD_JSON", "JSON 객체를 보내 주세요")
            return body

        def _gate(self):
            if app.data is None or not app.status["ok"]:
                self._send(503, {"error": "PIPELINE_CHECK_FAILED", "failures": app.status["failures"]})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            try:
                if path == "/api/status":
                    return self._send(200, app.api_status())
                if path == "/api/symbols":
                    return self._gate() and self._send(200, app.api_symbols())
                if path == "/api/trades":
                    return self._gate() and self._send(200, app.api_trades())
                return self._static(path)
            except SavedFileError as e:
                return self._send(500, {"error": "SAVED_FILE_BROKEN", "message": str(e)})
            except Exception as e:  # 그 밖의 오류도 연결을 끊지 않고 JSON으로 알립니다
                traceback.print_exc(file=sys.stderr)
                return self._send(500, {"error": "INTERNAL", "message": type(e).__name__})

        def do_PUT(self):
            if not self._host_ok():
                return
            if self.path != "/api/trades":
                return self._send(404, {"error": "NOT_FOUND"})
            if not self._gate():
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(415, {"error": "BAD_CONTENT_TYPE", "message": "application/json으로 보내 주세요"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "BAD_JSON", "message": "JSON을 읽지 못했습니다"})
            except BadRequest as e:
                return self._send(400, e.payload())
            try:
                return self._send(200, app.api_save_trades(body))
            except BadRequest as e:
                return self._send(400, e.payload())
            except SavedFileError as e:
                return self._send(500, {"error": "SAVED_FILE_BROKEN", "message": str(e)})
            except Exception as e:  # 그 밖의 오류도 연결을 끊지 않고 JSON으로 알립니다
                traceback.print_exc(file=sys.stderr)
                return self._send(500, {"error": "INTERNAL", "message": type(e).__name__})

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path != "/api/pnl":
                return self._send(404, {"error": "NOT_FOUND"})
            if not self._gate():
                return
            try:
                body = self._body()
            except ValueError:  # 길이 머리, JSON, UTF-8을 읽지 못함
                return self._send(400, {"error": "BAD_JSON", "message": "JSON을 읽지 못했습니다"})
            except BadRequest as e:
                return self._send(400, e.payload())
            try:
                return self._send(200, app.api_pnl(body))
            except BadRequest as e:
                return self._send(400, e.payload())
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


def serve(db, port=8765, trades=None):
    """서버를 띄우고 (서버, 스레드)를 돌려줍니다(port=0이면 빈 포트). 끌 때는 stop(서버).
    trades를 주면 화면에 넣은 매매를 그 파일에 저장합니다(주지 않으면 저장하지 않음, 대조 스크립트용)."""
    app = App(db, trades)
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
    ap.add_argument("--trades", default=str(TRADES), help="넣은 매매를 저장할 파일(data/generated/ 아래). 빈 값이면 저장 안 함")
    a = ap.parse_args()
    httpd, th = serve(a.db, a.port, a.trades or None)
    st = httpd.app.status
    try:
        warm = httpd.app.warm()
    except KeyboardInterrupt:
        stop(httpd)
        raise SystemExit(130) from None
    print(f"화면 서버: http://127.0.0.1:{httpd.server_address[1]}/  데이터 {Path(a.db).name}, "
          f"파이프라인 검사 {'통과' if st['ok'] else '실패: ' + ', '.join(f['name'] for f in st['failures'])}"
          + (f", 계산 준비 {warm}초" if warm is not None else ""))
    try:
        th.join()
    except KeyboardInterrupt:
        stop(httpd)
