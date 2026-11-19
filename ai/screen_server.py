"""F 화면의 계산 서버(docs/ai/F-screen.md 2절 「구조」). 파이썬 표준 라이브러리 HTTP 서버와 JSON API입니다.

사용: .venv/bin/python -m ai.screen_server [--db data/generated/screen.duckdb] [--port 8765]

API(127.0.0.1에서만 받고, Host 머리도 127.0.0.1이나 localhost만)
  GET  /api/status   10번 칸. 내보내기 검사와 지문 대조. ok가 거짓이면 아래 API는 503과 실패 항목만 돌려줍니다
  GET  /api/symbols  종목과 데이터 기간
  POST /api/pnl      {"symbol", "asof", "trades": [{"date", "side": "BUY"|"SELL"|"HOLD", "qty", "price"?, "fx_rate"?, "broker"?}]}
                     HOLD은 지금 잔고 줄(날짜 없음, 수량, 평균 단가, 미국 종목은 평균 매입 환율). broker는 증권사 코드(ai/holdings.py)
                     요약(2번 칸), 가격과 내 매매와 큰 움직임(3번), 누적 손익과 SPY 선(4번), 원화 수익 나누기(5번),
                     매도별 손익(6번), 경고(코드, 줄 번호, 문구). 한 줄의 입력이 틀리면 400과 그 줄 번호(trade_index).
                     금액이 손익 SQL의 decimal 범위를 넘으면(약 100억 원) 400 AMOUNT_TOO_LARGE와 금액이 가장 큰 줄
  GET  /api/range?symbol=&asof=  8번 칸(앞으로의 범위). 기준일 이전 마지막 거래일까지의 종가로 낸 20거래일 80% 범위와
                     지난날 적중률(n, 맞은 수, 창마다의 범위와 실제 값). 계산은 ai/screen_range.py
  POST /api/ask      9번 칸(묻고 답하기). {"question", "asof", "account": {종목: [매매 줄]}}. 화면에 넣은 매매가 「내 계좌」,
                     기준일이 「오늘」입니다. 답은 ai/qa.py(새 판 f1)이고 모델을 부릅니다(질문당 약 $0.01, 한 번에 하나).
                     돌려주는 것: status, text, numbers, dates_used, warnings, 쓴 도구, 코드가 정한 답인지, 시간, 비용.
                     이미 답을 만드는 중이면 429, 모델 호출 상한에 닿으면 503 BUDGET
  POST /api/events   7번 칸(그때 무슨 일이). {"symbol", "date"}. 그날의 움직임 문장(코드), 검색 판 H1이 고른 기사의 요약과
                     인용(ai/e02_answer.py, 모델 1 ~ 2번), 같은 날 일정(전날 장 마감부터 그날 장 마감 전까지 발표). 같은
                     종목과 날짜는 서버를 띄운 동안 한 번만 부르고 다시 쓰며, 국내 종목은 모델 없이 고정 문구. 한 번에 하나(429)
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
import hashlib
import json
import os
import shutil
import re
import statistics
import sys
import threading
import time
import traceback
from bisect import bisect_right
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import duckdb

from ai import holdings, llm, screen_data, screen_range
from ai.pnl import iso_date, number
from ai.tools import real_data

SAFE = (Path(__file__).resolve().parents[1] / "data" / "generated").resolve()

ROOT = Path(__file__).resolve().parents[1]
WARNINGS = json.loads((ROOT / "eval" / "f_screen" / "warnings.json").read_text(encoding="utf-8"))
STATIC = (ROOT / "screen").resolve()
TRADES = SAFE / "f_screen" / "screen_trades.json"
MAX_TRADES = 200
MAX_QUESTION = 300
# 7번 칸의 「같은 날 다른 자산」: 미국 증시, 나스닥, 장기 국채, 금, 달러, 비트코인, 원유
OTHERS = ["SPY", "QQQ", "TLT", "GLD", "UUP", "IBIT", "USO"]
MAX_ASKS = 200  # 서버를 한 번 띄운 동안 9번 칸이 물을 수 있는 수(모델 호출 상한과 따로 두는 화면 쪽 상한)
TRADE_KEYS = ("date", "side", "qty", "price", "fx_rate", "broker")
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


class Busy(Exception):
    """9번 칸: 이미 답을 만드는 중(모델 호출은 한 번에 하나)."""


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
        self.px = {}  # 8번 칸용 종목별 가격 목록(수정한 종가, 없으면 종가)
        self.ask_fn = None  # 9번 칸의 답하는 함수. 비우면 ai/qa.py ask(시험은 모델을 부르지 않는 함수로 바꿔 끼움)
        self.ask_lock = threading.Lock()
        self.events_fn = None  # 7번 칸의 답하는 함수. 비우면 ai/e02_answer.answer(시험은 바꿔 끼움)
        self.events_lock = threading.Lock()
        self.events_cache = {}  # (종목, 날짜) → 응답. 서버를 띄운 동안만
        self._news_con = None
        self.asks = 0
        # 화면 파일이 바뀌면(Airflow screen_export가 새 파일을 씀) 다음 요청에서 다시 읽습니다(maybe_reload).
        # DuckDB는 한 프로세스 안에서 같은 경로를 이미 열어 둔 연결이 있으면, 파일이 바뀌어도 새로 연 연결에 예전 내용을
        # 돌려줍니다(경로로 데이터베이스를 나눠 씀, 11/18에 확인). 그래서 다시 읽을 때는 새 파일을 하드 링크로 걸고
        # 그 경로로 엽니다. open_path가 지금 데이터를 연 경로입니다(기사 연결도 같은 경로로 엶).
        self.open_path = self.db
        self.sig = self._file_sig()
        self.reload_lock = threading.Lock()
        self.reloads, self.reloaded_at = 0, None
        self._retired = []  # (데이터, 기사 연결, 하드 링크). 쓰던 요청이 끝나도록 다음 다시 읽기 때 닫고 링크를 지움
        self._clean_links()

    def _link_glob(self):
        return f".{self.db.stem}.reload-*.duckdb"

    def _clean_links(self):
        """끝난 서버가 남긴 하드 링크를 지웁니다(이름의 프로세스 번호가 살아 있지 않은 것만)."""
        for p in self.db.parent.glob(self._link_glob()):
            try:
                os.kill(int(p.name.split("reload-")[1].split("-")[0]), 0)
            except (ProcessLookupError, ValueError, IndexError):
                p.unlink(missing_ok=True)
            except PermissionError:
                pass

    def _file_sig(self, path=None):
        try:
            st = (path or self.db).stat()
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def maybe_reload(self):
        """화면 파일이 바뀌었으면 다시 읽습니다. 요청마다 stat 한 번이라 가볍고, 다른 요청이 다시 읽는 중이면 기다리지 않고
        지금 데이터로 답합니다. 새 파일의 검사가 실패하면 화면이 숫자 대신 실패 항목을 보이고, 파일을 읽지 못하면
        「화면 파일을 다시 읽지 못함」을 실패로 둡니다(옛 숫자를 계속 보이지 않음). 파일이 잠깐 없으면 그대로 둡니다.
        돌려주는 값: 다시 읽었으면 True."""
        sig = self._file_sig()
        if sig is None or sig == self.sig or not self.reload_lock.acquire(blocking=False):
            return False
        try:
            if self._file_sig() is None or self._file_sig() == self.sig:
                return False
            link = self.db.with_name(f".{self.db.stem}.reload-{os.getpid()}-{self.reloads + 1}.duckdb")
            link.unlink(missing_ok=True)
            try:
                os.link(self.db, link)
            except OSError:
                shutil.copy2(self.db, link)  # 하드 링크를 못 거는 파일 시스템이면 복사
            sig = self._file_sig(link)
            try:
                new = real_data(link)
                status = new.status
            except Exception as e:  # noqa: BLE001 - 깨진 파일도 서버는 살아 있어야 함
                new, status = None, {**screen_data.MISSING, "failures": [
                    {"name": "화면 파일을 다시 읽지 못함", "ok": False, "value": type(e).__name__, "detail": str(e)[:200]}]}
            self._close_retired()
            self._retired = [(self.data, self._news_con, self.open_path if self.open_path != self.db else None)]
            self.data, self.status, self._news_con, self.open_path = new, status, None, link
            self.px, self.events_cache = {}, {}
            self.sig, self.reloads = sig, self.reloads + 1
            self.reloaded_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            print(f"화면 파일이 바뀌어 다시 읽음({self.reloads}번째): 파이프라인 검사 "
                  f"{'통과' if status.get('ok') else '실패: ' + ', '.join(f['name'] for f in status['failures'])}", flush=True)
            if new is not None and status.get("ok"):
                threading.Thread(target=self.warm, daemon=True).start()  # 첫 계산 준비(2 ~ 3초)를 뒤에서
            return True
        finally:
            self.reload_lock.release()

    def _close_retired(self):
        for data, con, link in self._retired:
            for x in (con, data):
                if x is not None:
                    x.close()
            if link is not None:
                link.unlink(missing_ok=True)
        self._retired = []

    def close(self):
        if self._news_con is not None:
            self._news_con.close()
            self._news_con = None
        if self.data:
            self.data.close()
        self._close_retired()
        if self.open_path != self.db:
            self.open_path.unlink(missing_ok=True)

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
        return {**self.status, "server_started": self.started, "db": self.db.name, "trades_lake": self._trades_lake(),
                "reloads": self.reloads, "reloaded_at": self.reloaded_at}

    def _trades_lake(self):
        """화면에 저장한 매매가 원천 → CDC → Gold(fct_execution)에 같은 값으로 닿았는지 마지막으로 본 결과
        (loaders/screen_trades.py --verify가 씀). 저장 끈 서버이거나 기록이 없으면 None.
        stale: 확인한 뒤 저장 파일이 바뀜(새로 고친 매매는 아직 옮기지 않음)."""
        if not self.trades_path:
            return None
        rec = self.trades_path.parent / "trades_lake.json"
        try:
            r = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        cur = hashlib.sha256(self.trades_path.read_bytes()).hexdigest() if self.trades_path.exists() else None
        keep = ("at", "account", "executions_want", "executions_gold", "executions_ok", "pnl_same", "pnl_checked",
                "gold_pnl_asof", "gold_pnl_same", "gold_pnl_checked", "path")
        return {**{k: r.get(k) for k in keep}, "stale": cur != r.get("trades_sha256")}

    def api_symbols(self):
        d = self.data
        out = []
        for s, v in sorted(d.symbols.items(), key=lambda kv: (kv[1]["market"] != "KRX", kv[0])):
            rows = d.rows[s]
            out.append({"symbol": s, "name": v["name"], "market": v["market"], "currency": v["currency"],
                        "first": rows[0], "last": rows[-1]})
        return {"symbols": out, "calendar": [d.calendar[0], d.calendar[-1]], "bench": d.bench,
                "brokers": [{"code": c, "name": n} for c, n in holdings.BROKERS]}

    def _trades(self, body, symbol=None):
        """매매 줄 검사. symbol을 주면 지금 잔고 줄의 매입 환율(미국 종목)이 있는지도 봅니다."""
        trades = body.get("trades")
        if not isinstance(trades, list) or not trades:
            raise BadRequest("NO_TRADES", "매매 줄이 없습니다")
        if len(trades) > MAX_TRADES:
            raise BadRequest("TOO_MANY_TRADES", f"매매 줄은 {MAX_TRADES}개까지입니다")
        out, holds = [], set()
        usd = symbol is not None and self.data.symbols[symbol]["currency"] == "USD"
        for k, t in enumerate(trades):
            n = k + 1  # 문구의 줄 번호는 1부터, trade_index는 0부터
            if not isinstance(t, dict):
                raise BadRequest("BAD_TRADE", f"{n}번 줄의 모양이 틀렸습니다", k)
            side = str(t.get("side", "")).upper()
            if side not in ("BUY", "SELL", "HOLD"):
                raise BadRequest("BAD_TRADE", f"{n}번 줄의 매매는 BUY, SELL, HOLD(지금 잔고)입니다", k)
            broker = t.get("broker") or ""
            if broker and broker not in holdings.BROKER_NAME:
                raise BadRequest("BAD_TRADE", f"{n}번 줄의 증권사를 모릅니다: {str(broker)[:20]}", k)
            if side == "HOLD":
                # 지금 잔고 줄: 날짜는 쓰지 않음(증권사 앱 잔고를 본 때). 증권사마다 하나
                if broker in holds:
                    raise BadRequest("BAD_TRADE", f"{n}번 줄: {holdings.broker_name(broker)}의 지금 잔고 줄이 둘입니다", k)
                holds.add(broker)
                row = {"d": None, "side": side}
            else:
                if not iso_date(t.get("date")):
                    raise BadRequest("BAD_DATE", f"{n}번 줄의 날짜는 YYYY-MM-DD로 주세요", k)
                row = {"d": t["date"], "side": side}
            if broker:
                row["broker"] = broker
            hold_label = {"price": "평균 단가", "fx_rate": "평균 매입 환율"}
            for key, name, label in (("qty", "q", "수량"), ("price", "p", "체결가"), ("fx_rate", "fx", "환율")):
                if side == "HOLD":
                    label = hold_label.get(key, label)
                v = t.get(key)
                if v is None or v == "":
                    if key == "qty":
                        raise BadRequest("BAD_TRADE", f"{n}번 줄의 수량이 없습니다", k)
                    if side == "HOLD" and (key == "price" or (key == "fx_rate" and usd)):
                        raise BadRequest("BAD_TRADE", f"{n}번 줄(지금 잔고)의 {label}이 없습니다", k)
                    continue
                try:
                    v = number(v, {"q": "qty", "p": "price", "fx": "fx"}[name])
                except ValueError as e:
                    raise BadRequest("BAD_TRADE", f"{n}번 줄의 {label}: {e}", k) from None
                if side == "HOLD" and name == "q" and (v <= 0 or v != v.to_integral_value()):
                    raise BadRequest("BAD_TRADE", f"{n}번 줄(지금 잔고)의 수량은 0보다 큰 정수입니다", k)
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
        return {**w, "trade_date": t["d"] or "지금 잔고", "message": WARNINGS[w["code"]].format(**fill), **fill}

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
        trades = self._trades(body, symbol)
        t0 = time.perf_counter()
        try:
            with d.lock:
                r = holdings.run(d.engine(), d.world, symbol, trades, asof, d.bench)
        except duckdb.OutOfRangeException:
            # 손익 SQL의 decimal 곱셈이 넘침(매매 금액이나 기준일 평가액이 약 100억 원). 가장 큰 매매를 짚어 줍니다
            k = max(range(len(trades)), key=lambda i: self._amount(symbol, trades[i]))
            raise BadRequest("AMOUNT_TOO_LARGE",
                             f"금액이 계산할 수 있는 범위(매매 한 건이나 기준일 평가액이 약 100억 원)를 넘었습니다. "
                             f"금액이 가장 큰 {k + 1}번 줄에서 수량, 체결가, 환율의 단위를 확인하세요", k) from None
        rows = d.symbols[symbol]["rows"]
        currency = d.symbols[symbol]["currency"]
        summary, sells, warnings = r["summary"], r["sells"], r["warnings"]
        kept, (daily, steps) = r["combined"]
        if summary:
            summary = {k: _num(v) for k, v in summary.items() if not k.startswith("gap_")}
            summary["total_minus_bench_krw"] = summary["total_krw"] - summary["bench_total_krw"]
            summary["naive_minus_total_krw"] = summary["naive_krw"] - summary["total_krw"]
            if not r["multi"]:
                summary["holding_return_pct"], summary["holding_return_krw_pct"] = holdings.holding_return(summary, currency)
            else:
                summary["holding_return_pct"] = summary["holding_return_krw_pct"] = None
        # 첫 매수일: 합친 장부의 첫 매수(시작 보유면 데이터 첫날)
        buys = [kept[seq] for seq, _, side, *_ in steps if side == "BUY"]
        holding = [trades[buys[0]]["d"] or d.rows[symbol][0], asof] if buys else None
        # 연 환산이 없는 까닭(pnl_irr): 첫 매매일부터 기준일(달력에 맞춘 날)까지 30일 미만인지
        live = [cd for _, cd, _, _, _, _, over, *_ in steps if not over]
        end = _last_on_or_before(d.calendar, asof)
        irr_short = bool(live) and (date.fromisoformat(end) - min(live)).days < 30
        accounts = [{**a, "summary": {k: _num(v) for k, v in a["summary"].items()} if a["summary"] else None,
                     "opening": {k: _num(v) if k != "date" else v for k, v in a["opening"].items()} if a["opening"] else None}
                    for a in r["accounts"]]
        took = time.perf_counter() - t0
        return {
            "symbol": symbol, "name": d.symbols[symbol]["name"], "currency": d.symbols[symbol]["currency"],
            "asof": asof, "asof_price_date": _last_on_or_before(d.rows[symbol], asof), "bench": d.bench,
            "summary": summary or None, "booked": bool(buys),
            "sells": [{k: (_num(v) if k != "broker" else v) for k, v in s.items()} for s in sells],
            "warnings": [self._message(w, symbol, trades) for w in warnings],
            # 매매 줄마다(증권사별 장부): 처리 뒤 수량과 평균 단가. 시작 보유(opening)는 지금 잔고 줄의 번호
            "steps": [{"trade_index": x["trade_index"], "broker": x["broker"], "opening": x["opening"],
                       "date": cd.isoformat(), "side": side, "qty": q, "price": _num(p),
                       "fx": _num(fx), "excluded": bool(over), "qty_after": qa,
                       "avg_cost_after": _round(cost / qa, 4) if qa else None}
                      for x in r["steps"] for seq, cd, side, q, p, fx, over, qa, cost in [x["row"]]],
            "accounts": accounts, "notes": r["notes"], "multi": r["multi"], "opening": r["opening"],
            "daily": [{"date": c.isoformat(), "pnl_krw": won(p), "peak_krw": won(pk), "bench_krw": won(b)}
                      for c, p, pk, b in daily],
            "prices": [[x, _num(rows[x]["close"])] for x in d.rows[symbol]],
            "big_moves": big_moves(rows, holding[0], holding[1]) if holding else None,
            "holding": holding,
            "irr_short": irr_short,
            "seconds": round(took, 4),
            "ms": round(took * 1000),
        }


    def api_range(self, query):
        """8번 칸. query는 parse_qs 결과. 기준일은 데이터 기간 안이어야 하고, 그 종목의 기준일 이전 마지막 거래일로 셉니다."""
        d = self.data
        symbol = (query.get("symbol") or [None])[0]
        if not symbol:
            raise BadRequest("UNKNOWN_SYMBOL", "종목을 주세요")
        if symbol not in d.symbols:
            raise BadRequest("UNKNOWN_SYMBOL", f"없는 종목입니다: {symbol}")
        asof = (query.get("asof") or [None])[0]
        if not iso_date(asof):
            raise BadRequest("BAD_DATE", "기준일은 YYYY-MM-DD로 주세요")
        if not d.calendar[0] <= asof <= d.calendar[-1]:
            raise BadRequest("OUT_OF_DATA_RANGE", f"기준일은 데이터 기간({d.calendar[0]} ~ {d.calendar[-1]}) 안이어야 합니다")
        days = d.rows[symbol]
        t = bisect_right(days, asof) - 1
        if t < 0:
            raise BadRequest("OUT_OF_DATA_RANGE", "기준일 앞에 이 종목의 거래일이 없습니다")
        if symbol not in self.px:
            rows = d.symbols[symbol]["rows"]
            self.px[symbol] = [float(rows[x]["adj_close"] if rows[x].get("adj_close") is not None else rows[x]["close"])
                               for x in days]
        t0 = time.perf_counter()
        v = screen_range.view(days, self.px[symbol], t)
        return {"symbol": symbol, "name": d.symbols[symbol]["name"], "currency": d.symbols[symbol]["currency"],
                "asof": asof, **v, "ms": round((time.perf_counter() - t0) * 1000)}

    def api_events(self, body):
        """7번 칸. 그 종목의 거래일 하루에 대해 움직임 문장, 기사 요약과 인용, 같은 날 일정. 국내 종목은 고정 문구."""
        from ai import e02_answer as A
        from ai import news_search
        from ai.tools import SW
        d = self.data
        symbol = body.get("symbol")
        if not isinstance(symbol, str) or symbol not in d.symbols:
            raise BadRequest("UNKNOWN_SYMBOL", f"없는 종목입니다: {symbol}")
        day = body.get("date")
        if not iso_date(day):
            raise BadRequest("BAD_DATE", "날짜는 YYYY-MM-DD로 주세요")
        rows = d.symbols[symbol]["rows"]
        if day not in rows:
            raise BadRequest("NOT_TRADING_DAY", f"{day}는 {d.symbols[symbol]['name']}({symbol})의 거래일이 아닙니다")
        name = d.symbols[symbol]["name"]
        key = (symbol, day)
        if key in self.events_cache:
            return {**self.events_cache[key], "cached": True}
        if symbol not in A.NAMES:  # 국내 2종목: E02 1판 범위 밖(설계서 2절 7번 칸)
            mv = A.day_move(rows, day)
            out = {"symbol": symbol, "name": name, "date": day, "status": "unknown", "head": A.move_line(symbol, day, mv, name),
                   "body": A.TEXT["domestic"], "move": mv, "citations": [], "schedule": [], "decided_by": "code:domestic",
                   "n_llm": 0, "seconds": 0, "cost_usd": 0, "version": None}
            self.events_cache[key] = out
            return {**out, "cached": False}
        days = [date.fromisoformat(x) for x in d.us_days]
        t = date.fromisoformat(day)
        i = days.index(t) if t in days else -1
        if i <= 0 or i + 1 >= len(days):
            raise BadRequest("NO_WINDOW", "데이터의 첫 거래일과 마지막 거래일은 검색 창(전날 장 마감부터 다음 날 장 시작 전까지)을 "
                                          "다 채우지 못해 보이지 않습니다")
        if self.asks >= MAX_ASKS:
            raise Busy(f"이번 서버 실행에서 모델을 부를 수 있는 수({MAX_ASKS}번)를 넘었습니다. 서버를 다시 띄우세요")
        if not self.events_lock.acquire(blocking=False):
            raise Busy("앞 날짜의 기사를 정리하는 중입니다. 끝난 뒤 다시 눌러 주세요")
        self.asks += 1
        try:
            if self._news_con is None:
                self._news_con = duckdb.connect(str(self.open_path), read_only=True)
            cur = self._news_con.cursor()
            try:
                fn = self.events_fn or A.answer
                ans, trace = fn(cur, days, rows, symbol, day, "h1", tag=f"screen-e02:{symbol}:{day}")
                start, _ = SW.window(days, t)
                close = datetime.combine(t, datetime.min.time(), SW.ET) + timedelta(hours=13 if t in SW.HALF_DAYS else 16)
                schedule = d.events_fn(day, start, close)
            finally:
                cur.close()
        finally:
            self.events_lock.release()
        calls = trace.get("llm", [])
        others = []  # 같은 날 다른 자산의 움직임(코드). 같이 움직였다는 것이지 원인이 아님
        for o in OTHERS:
            orows = d.symbols.get(o, {}).get("rows", {})
            if o == symbol or day not in orows:
                continue
            m = A.day_move(orows, day)
            others.append({"symbol": o, "name": d.symbols[o]["name"], "pct": m["pct"], "ratio": m["ratio"], "big": m["big"]})
        out = {"symbol": symbol, "name": name, "date": day, "status": ans["status"], "head": ans.get("head"),
               "body": ans.get("body"), "move": ans["move"], "citations": ans["citations"], "schedule": schedule,
               "decided_by": trace.get("decided_by"), "n_llm": len(calls), "seconds": round(trace.get("seconds", 0), 1),
               "cost_usd": round(sum(c.get("cost_usd") or 0 for c in calls), 4), "version": "h1",
               "window": [x.isoformat() for x in SW.window(days, t)], "window_articles": trace.get("window_articles"),
               "others": others}
        if not any(c.get("error") for c in calls):  # 모델 호출이 실패한 답은 다시 부를 수 있게 기억하지 않음
            self.events_cache[key] = out
        return {**out, "cached": False}

    def api_ask(self, body):
        """9번 칸. 화면에 넣은 매매(account)를 계좌로, 기준일(asof)을 오늘로 묻고 답하기 새 판을 부릅니다."""
        d = self.data
        q = body.get("question")
        if not isinstance(q, str) or not q.strip():
            raise BadRequest("NO_QUESTION", "질문을 적어 주세요")
        q = q.strip()
        if len(q) > MAX_QUESTION:
            raise BadRequest("TOO_LONG", f"질문은 {MAX_QUESTION}자까지입니다")
        asof = body.get("asof")
        if not iso_date(asof):
            raise BadRequest("BAD_DATE", "기준일은 YYYY-MM-DD로 주세요")
        if not d.calendar[0] <= asof <= d.calendar[-1]:
            raise BadRequest("OUT_OF_DATA_RANGE", f"기준일은 데이터 기간({d.calendar[0]} ~ {d.calendar[-1]}) 안이어야 합니다")
        acct = body.get("account") or {}
        if not isinstance(acct, dict):
            raise BadRequest("BAD_ACCOUNT", "넣은 매매의 모양이 틀렸습니다")
        trades = {}
        for sym, rows in acct.items():
            if sym not in d.symbols:
                raise BadRequest("UNKNOWN_SYMBOL", f"넣은 매매에 없는 종목이 있습니다: {sym}")
            if not isinstance(rows, list):
                raise BadRequest("BAD_ACCOUNT", f"{d.symbols[sym]['name']}({sym}) 매매의 모양이 틀렸습니다")
            if not rows:
                continue
            try:
                parsed = self._trades({"trades": rows}, sym)
                # 지금 잔고 줄과 증권사는 합친 매매로 바꿔 넘김(묻고 답하기는 한 계좌로 셈, 7절 「증권사와 지금 잔고」)
                with d.lock:
                    trades[sym] = holdings.effective(d.engine(), d.world, sym, parsed, d.bench)
            except BadRequest as e:
                # 화면은 날짜와 수량이 있는 줄만 보내므로 「n번 줄」이 화면의 줄 번호와 다를 수 있어 번호를 뺌
                msg = re.sub(r"^\d+번 줄의 ", "", e.message)
                raise BadRequest(e.code, f"{d.symbols[sym]['name']}({sym}) 매매에 틀린 줄이 있습니다: {msg}") from None
        if self.asks >= MAX_ASKS:
            raise Busy(f"이번 서버 실행에서 물을 수 있는 수({MAX_ASKS}번)를 넘었습니다. 서버를 다시 띄우세요")
        if not self.ask_lock.acquire(blocking=False):
            raise Busy("앞 질문의 답을 만드는 중입니다. 답이 나온 뒤 다시 물어 주세요")
        self.asks += 1
        try:
            fn = self.ask_fn
            if fn is None:
                from ai import qa
                fn = qa.ask
            from ai.tools import Toolset
            t0 = time.perf_counter()
            ans, trace = fn(Toolset(d, asof, {"id": "screen", "trades": trades}), {"id": "screen", "text": q, "today": asof})
        finally:
            self.ask_lock.release()
        calls = trace.get("llm", [])
        return {"question": q, "asof": asof, **{k: ans.get(k) for k in ("status", "text", "numbers", "dates_used", "warnings")},
                "tools": [c["name"] for c in trace.get("tool_calls", [])], "decided_by": trace.get("decided_by"),
                "rewrite": bool(trace.get("rewrite")), "n_llm": len(calls),
                "cost_usd": round(sum(c.get("cost_usd") or 0 for c in calls), 4),
                "seconds": round(time.perf_counter() - t0, 1)}

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
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # 받는 쪽이 이미 떠남(화면이 요청을 취소했거나 창을 닫음). 보낼 곳이 없으니 조용히 넘깁니다
                self.close_connection = True
            return None

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
            app.maybe_reload()
            path = self.path.split("?")[0]
            try:
                if path == "/api/status":
                    return self._send(200, app.api_status())
                if path == "/api/symbols":
                    return self._gate() and self._send(200, app.api_symbols())
                if path == "/api/trades":
                    return self._gate() and self._send(200, app.api_trades())
                if path == "/api/range":
                    if not self._gate():
                        return None
                    try:
                        return self._send(200, app.api_range(parse_qs(urlsplit(self.path).query)))
                    except BadRequest as e:
                        return self._send(400, e.payload())
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
            app.maybe_reload()
            if self.path not in ("/api/pnl", "/api/ask", "/api/events"):
                return self._send(404, {"error": "NOT_FOUND"})
            if not self._gate():
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(415, {"error": "BAD_CONTENT_TYPE", "message": "application/json으로 보내 주세요"})
            try:
                body = self._body()
            except ValueError:  # 길이 머리, JSON, UTF-8을 읽지 못함
                return self._send(400, {"error": "BAD_JSON", "message": "JSON을 읽지 못했습니다"})
            except BadRequest as e:
                return self._send(400, e.payload())
            try:
                fn = {"/api/ask": app.api_ask, "/api/events": app.api_events, "/api/pnl": app.api_pnl}[self.path]
                return self._send(200, fn(body))
            except BadRequest as e:
                return self._send(400, e.payload())
            except Busy as e:
                return self._send(429, {"error": "BUSY", "message": str(e)})
            except llm.BudgetExceeded:
                return self._send(503, {"error": "BUDGET", "message": "모델 호출 상한에 닿아 답할 수 없습니다"})
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
