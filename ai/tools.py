"""E04 도구 7개(docs/ai/experiments/E04-agent.md 3절 「도구」)를 데이터 원천을 넘겨받는 층으로 나눈 판입니다.

- Toolset(data, today, account): data는 fake_data()(E04 가짜 세계 eval/e04_agent/world.json) 또는
  real_data(화면 데이터 파일 data/generated/screen.duckdb). today는 기준일, account는 내 매매.
- 모듈 수준의 TOOLS, call(), WORLD, ACCOUNT, SYMBOLS, ROWS, FX_DAYS, TODAY는 가짜 세계 판입니다. E04 에이전트
  (ai/agent.py)가 그대로 씁니다.
- 손익(portfolio_pnl, whatif_pnl)은 제품 SQL(ai/pnl.py compute)로 셉니다. 시세와 환율은 원천 값을 그대로 읽고,
  쉬는 날이면 직전 거래일로 맞춘 뒤 NON_TRADING_DAY를 붙입니다.
- 반올림은 E01 R15(0.5는 0에서 먼 쪽)입니다. 둘 이상을 비교하는 값(차이, 합계)도 도구가 내어, 모델이 직접 계산하지 않게 합니다.
- 실패는 예외 대신 {"error": 코드, "message": 설명}으로 돌려줍니다.

F 화면에서 바꾼 일곱 가지(docs/ai/F-screen.md 2절 「도구 층에서 바꾸는 일곱 가지」):
1. 내 계좌와 오늘은 Toolset이 받은 account와 today입니다. 종목의 마지막 시세 날보다 뒤를 물으면 마지막 날 값과
   LATEST_NOT_LOADED를 냅니다(국내 시세는 다음 날 오후에 들어옴).
2. 기사는 E02 검색 창(eval/e02_rag/search_window.py: 전날 장 마감부터 다음 날 09:30 전까지, 미국 동부 시각)으로
   고릅니다. 쉬는 날을 물으면 직전 거래일의 창(주말 기사가 들어 있음)을 씁니다.
3. 날짜 맞춤은 종목의 시세 날짜(그 시장의 달력)로 합니다. 손익 SQL에 넣는 달력은 원천의 합집합 그대로입니다.
4. 소수 수량은 자르지 않고 그 줄을 빼며 FRACTIONAL_QTY를 냅니다. 손익은 화면과 같은 계산(ai/pnl.py compute)이라
   화면 경고 셋(OUT_OF_DATA_RANGE, FRACTIONAL_QTY, FX_CARRIED)이 도구 결과에도 붙습니다. 화면과 묻고 답하기의
   숫자와 경고가 한 계산에서 나오게 하려는 것입니다(E04 기록 재생에서 FX_CARRIED가 더해진 것은 의도한 변경).
5. 이름이 같고 정의가 다른 값을 나눕니다. 기간 통계는 price_max_drawdown_pct(가격 기준 %, 손익의 max_drawdown_krw와 다름),
   largest_day_move_date와 largest_day_move_pct(기간에서 가장 큰 하루 움직임, 3번 칸의 큰 움직임과 다름)입니다.
6. 기준 종목은 원천이 정합니다(가짜 BENCH, 실제 SPY).
7. 모델 호출 장부와 실행 기록의 자리는 Data.kind("fake", "real")로 정합니다(ai/llm.py의 ledger_for, runs_dir).
   실제 데이터는 data/generated/(git 제외) 밖에 쓰면 멈춥니다.

6단계(묻고 답하기 새 판)에서 더한 것: range 도구. 화면 8번 칸과 같은 계산(ai/screen_range.py, E03 기준선 「최근 20일
변동성」)으로 앞으로 20거래일 80% 범위, 지난 창 n과 맞은 수, 적중률, 표시 갈래를 냅니다. 오늘(기준일)까지의 가격만
넘겨 뒤 값을 읽을 수 없게 합니다. similar_periods(비슷했던 때)는 가짜 세계에만 남습니다(F-screen.md 2절).
"""
import importlib.util
import json
import threading
from bisect import bisect_right
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb

from ai import screen_range
from ai.pnl import Engine, compute, iso_date, number

ROOT = Path(__file__).resolve().parents[1]
NEWS_LIMIT = 30


def _search_window():
    spec = importlib.util.spec_from_file_location("search_window", ROOT / "eval" / "e02_rag" / "search_window.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SW = _search_window()


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _num(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _clean(d):
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_clean(v) for v in d]
    return _num(d)


def _signed(v):
    """화면과 같은 % 표기: 0보다 크면 +, 작으면 −(U+2212), 소수 둘째 자리까지 채움(값은 이미 반올림됨)."""
    sign = "+" if v > 0 else "\u2212" if v < 0 else ""
    return f"{sign}{abs(v):.2f}"


def _r2(x):
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _snap(days, d):
    """d와 같거나 앞선 마지막 날."""
    i = bisect_right(days, d)
    if i == 0:
        raise ToolError("OUT_OF_RANGE", f"{d} 이전 값이 없습니다")
    return days[i - 1]


class Data:
    """도구가 읽는 원천: 시세, 환율, 달력, 기사, 일정과 손익 엔진(한 번 만들어 나눠 씀).
    kind는 "fake"(E04 가짜 세계) 또는 "real"(실제 시세). 모델 호출 장부와 실행 기록의 위치를 정합니다(바꿀 것 7,
    ai/llm.py의 ledger_for, runs_dir)."""

    def __init__(self, world, bench, news_fn, events_fn, kind, regime=None, status=None, close=None, search_fn=None):
        self.world, self.bench, self.regime, self.kind = world, bench, regime, kind
        self.search_fn = search_fn  # 실제 데이터의 E02 검색 판(ai/news_search.py). 가짜 세계는 None(시각 순 기사)
        self.symbols, self.calendar, self.usdkrw = world["symbols"], world["calendar"], world["usdkrw"]
        self.rows = {s: sorted(v["rows"]) for s, v in self.symbols.items()}
        self.fx_days = sorted(self.usdkrw)
        self.us_days = sorted({d for v in self.symbols.values() if v["currency"] == "USD" for d in v["rows"]})
        self.news_fn, self.events_fn = news_fn, events_fn
        self.status = status
        self._close = close
        self.lock = threading.Lock()
        self._engine = None

    def engine(self):
        if self._engine is None:
            self._engine = Engine(self.world)
        return self._engine

    def close(self):
        if self._close:
            self._close()
            self._close = None


def fake_data(path=ROOT / "eval" / "e04_agent" / "world.json"):
    """E04 가짜 세계. 기사 시각은 미국 동부 시각(오프셋 포함)이고, 일정은 날짜만 있습니다."""
    world = json.loads(Path(path).read_text(encoding="utf-8"))

    def news(start, end, symbol):
        out = []
        for n in world["news"]:
            at = datetime.fromisoformat(n["at"])
            if start <= at < end and (symbol is None or symbol in n["symbols"]):
                out.append({"id": n["id"], "time": n["at"], "symbols": n["symbols"], "title": n["title"],
                            "summary": n["summary"]})
        return out

    def events(d, start=None, close=None):  # 가짜 세계의 일정은 날짜만 있어 날짜로 고름(E04와 같음)
        return [{"id": e["id"], "date": e["date"], "kind": e["kind"], "title": e["title"]}
                for e in world["events"] if e["date"] == d]

    return Data(world, "BENCH", news, events, "fake", regime=world["regime"]), world


def real_data(path=None):
    """화면 데이터 파일(ai/screen_data.py가 내보냄). 연결 하나로 검사와 지문을 대조하고(status) 시세와 환율을
    메모리로 읽습니다. 기사와 일정은 열어 둔 같은 연결로 그때그때 고릅니다(close()로 닫음)."""
    from ai import screen_data
    path = Path(path or screen_data.OUT)
    con = duckdb.connect(str(path), read_only=True)
    try:
        status = screen_data.status_of(con)
        world = screen_data.world_of(con)
    except BaseException:
        con.close()
        raise
    lock = threading.Lock()

    def news(start, end, symbol):
        utc = lambda t: t.astimezone(timezone.utc).replace(tzinfo=None)
        sql = ("select id, published_utc, tickers, title, description from news "
               "where published_utc >= ? and published_utc < ?")
        args = [utc(start), utc(end)]
        if symbol:
            sql += " and list_contains(tickers, ?)"
            args.append(symbol)
        with lock:
            rows = con.execute(sql + " order by published_utc, id limit ?", args + [NEWS_LIMIT + 1]).fetchall()
        return [{"id": i, "time": t.isoformat() + "Z", "symbols": list(tk), "title": ti, "summary": de}
                for i, t, tk, ti, de in rows]

    def events(d, start=None, close=None):
        """start와 close를 주면 발표 시각(현지 시각과 시간대)이 [start, close)인 일정. 휴장일(성금요일 고용 보고서 등)의
        일정이 다음 거래일 쪽에 듦. 주지 않으면 그날 날짜의 일정."""
        if start is None:
            with lock:
                rows = con.execute("select id, event_date, kind, title from events where event_date = ? order by id",
                                   [d]).fetchall()
            return [{"id": i, "date": e.isoformat(), "kind": k, "title": t} for i, e, k, t in rows]
        with lock:
            got = con.execute("select id, event_date, kind, title, local_time, tz from events "
                              "where event_date between ? and ? order by event_date, id",
                              [start.date(), close.date()]).fetchall()
        rows = []
        for i, e, k, t, lt, tz in got:
            at = datetime.combine(e, datetime.strptime(lt, "%H:%M").time(), ZoneInfo(tz))
            if start <= at < close:
                rows.append((i, e, k, t))
        return [{"id": i, "date": e.isoformat(), "kind": k, "title": t} for i, e, k, t in rows]

    def search(t, symbol, version="h1", extra=()):
        """E02 검색 판의 상위 5건. extra는 평가에서만 넣는 기사(주입 문장 시험)로, 창 기사에 더한 뒤 순위를 셈."""
        from ai import news_search
        with lock:
            arts, _ = news_search.window_articles(con, [Date.fromisoformat(x) for x in world_days], t)
        arts = arts + [dict(a) for a in extra]
        return news_search.VERSIONS[version](arts, symbol)

    world_days = sorted({d for v in world["symbols"].values() if v["currency"] == "USD" for d in v["rows"]})
    return Data(world, world["bench"], news, events, "real", status=status, close=con.close, search_fn=search)


class Toolset:
    def __init__(self, data, today, account):
        self.data, self.today, self.account = data, today, account
        self.first = data.calendar[0]
        self.extra_news = []  # 평가의 주입 문장 시험에서만 채움(eval/f_screen/run_qa.py)
        self.tools = {f: getattr(self, f) for f in
                      ("portfolio_pnl", "whatif_pnl", "price", "fx", "period_stats", "search_events", "similar_periods",
                       "range")}

    # ---------- 공통 ----------

    def _symbol(self, s):
        syms = self.data.symbols
        if s in syms:
            return s
        for code, v in syms.items():
            if s == v["name"]:
                return code
        raise ToolError("UNKNOWN_SYMBOL", f"없는 종목입니다: {s}. 있는 종목: " +
                        ", ".join(f"{k}({v['name']})" for k, v in syms.items()))

    def _date(self, d):
        if not iso_date(d):
            raise ToolError("BAD_DATE", f"날짜는 YYYY-MM-DD로 주세요: {d}")
        if d > self.today:
            raise ToolError("FUTURE_DATE", f"{d}는 오늘({self.today}) 뒤라 값이 없습니다")
        if d < self.first:
            raise ToolError("OUT_OF_RANGE", f"{d}는 데이터 범위({self.first} ~ {self.today}) 밖입니다")
        return d

    def _close(self, s, d):
        return Decimal(str(self.data.symbols[s]["rows"][d]["close"]))

    def _day_move(self, s, d):
        rows = self.data.rows[s]
        i = rows.index(d)
        if i == 0:
            return None
        return _r2((self._close(s, d) / self._close(s, rows[i - 1]) - 1) * 100)

    def _date_warnings(self, s, asked, used):
        """쉬는 날이면 NON_TRADING_DAY, 종목의 마지막 시세 날보다 뒤면 LATEST_NOT_LOADED(바꿀 것 1)."""
        if used == asked:
            return []
        return ["LATEST_NOT_LOADED"] if asked > self.data.rows[s][-1] else ["NON_TRADING_DAY"]

    def _pnl(self, symbol, trades):
        d = self.data
        with d.lock:
            summary, sells, warnings, _ = compute(d.engine(), d.world, symbol, trades, self.today, d.bench)
        rows = d.rows[symbol]
        for w in warnings:
            t = trades[w["trade_index"]]
            w["trade_date"] = t["d"]
            if w["code"] == "NON_TRADING_DAY":
                w["price_date_used"] = _snap(rows, t["d"])
            elif w["code"] == "OUT_OF_DATA_RANGE":
                w["data_range"] = [rows[0], rows[-1]]
            elif w["code"] == "FX_CARRIED":
                w["fx_date_used"] = _snap(d.fx_days, _snap(d.calendar, t["d"]))
        if summary:
            summary["total_minus_bench_krw"] = summary["total_krw"] - summary["bench_total_krw"]
            summary["naive_minus_total_krw"] = summary["naive_krw"] - summary["total_krw"]
        v = d.symbols[symbol]
        return {"symbol": symbol, "name": v["name"], "currency": v["currency"], "asof": self.today,
                "summary": summary, "sells": sells, "warnings": warnings}

    # ---------- 도구 ----------

    def portfolio_pnl(self, symbol=None, account=None):
        """내 계좌의 종목별 손익. symbol이 없으면 모든 종목과 계좌 합계."""
        if account not in (None, "", self.account["id"]):
            raise ToolError("NOT_YOUR_ACCOUNT", "다른 계좌는 조회할 수 없습니다")
        mine = self.account["trades"]
        symbols = [self._symbol(symbol)] if symbol else list(mine)
        out = {"account": self.account["id"], "positions": []}
        for s in symbols:
            if s not in mine:
                out["positions"].append({"symbol": s, "name": self.data.symbols[s]["name"],
                                         "note": "이 계좌에 매매가 없습니다"})
                continue
            r = self._pnl(s, mine[s])
            r["my_trades"] = [{"date": t["d"], "side": t["side"], "qty": t["q"]} for t in mine[s]]
            out["positions"].append(r)
        if not symbol:
            out["account_total_krw"] = sum(p["summary"]["total_krw"] for p in out["positions"] if p.get("summary"))
        return out

    def whatif_pnl(self, symbol, trades=(), use_my_trades=False, drop_trade_dates=()):
        """가정한 매매의 손익. trades: [{"date", "side": "BUY"|"SELL", "qty", "price"(선택), "fx_rate"(선택)}].
        use_my_trades면 내 실제 매매에서 drop_trade_dates의 줄을 빼고 trades를 더합니다.
        소수 수량은 자르지 않고 그 줄을 빼며 FRACTIONAL_QTY 경고를 냅니다(바꿀 것 4)."""
        s = self._symbol(symbol)
        rows = []
        if use_my_trades:
            drop = set(drop_trade_dates or ())
            rows = [dict(t) for t in self.account["trades"].get(s, []) if t["d"] not in drop]
        for t in trades or ():
            side = str(t.get("side", "")).upper()
            if side not in ("BUY", "SELL"):
                raise ToolError("BAD_TRADE", f"side는 BUY나 SELL입니다: {t}")
            try:
                q = number(t.get("qty", 0), "qty")
                row = {"d": self._date(t.get("date")), "side": side,
                       "q": int(q) if q == q.to_integral_value() else q}
                if t.get("price") is not None:
                    row["p"] = str(number(t["price"], "price"))
                if t.get("fx_rate") is not None:
                    row["fx"] = str(number(t["fx_rate"], "fx"))
            except ValueError as e:
                raise ToolError("BAD_TRADE", f"{e}: {t}") from None
            rows.append(row)
        if not rows:
            raise ToolError("NO_TRADES", "계산할 매매가 없습니다")
        rows.sort(key=lambda t: t["d"])
        r = self._pnl(s, rows)
        r["trades_used"] = [{"date": t["d"], "side": t["side"], "qty": t["q"], **({"price": t["p"]} if "p" in t else {}),
                             **({"fx_rate": t["fx"]} if "fx" in t else {})} for t in rows]
        return r

    def price(self, symbol, date):
        """그날 종가. 쉬는 날이면 직전 거래일 값과 NON_TRADING_DAY."""
        s, d = self._symbol(symbol), self._date(date)
        used = _snap(self.data.rows[s], d)
        row = self.data.symbols[s]["rows"][used]
        v = self.data.symbols[s]
        return {"symbol": s, "name": v["name"], "currency": v["currency"], "date_asked": d,
                "date_used": used, "close": row["close"], "low": row["low"], "high": row["high"],
                "day_move_pct": self._day_move(s, used), "warnings": self._date_warnings(s, d, used)}

    def fx(self, date):
        """원/달러 매매기준율. 쉬는 날이면 직전 날 값과 NON_TRADING_DAY."""
        d = self._date(date)
        used = _snap(self.data.fx_days, d)
        return {"pair": "USDKRW", "date_asked": d, "date_used": used, "rate": self.data.usdkrw[used],
                "warnings": [] if used == d else ["NON_TRADING_DAY"]}

    def period_stats(self, symbol, start, end=None):
        """start 종가 대비 end 종가의 수익률(%), 기간의 가격 기준 최대 낙폭(%), 하루 움직임이 가장 컸던 날.
        "올해 들어"처럼 기준이 직전 해 마지막 거래일이면 그 날을 start로 줍니다."""
        s = self._symbol(symbol)
        rows = self.data.rows[s]
        a, b = _snap(rows, self._date(start)), _snap(rows, self._date(end or self.today))
        if a > b:
            raise ToolError("BAD_PERIOD", "시작이 끝보다 늦습니다")
        days = [d for d in rows if a <= d <= b]
        peak, mdd = Decimal(0), Decimal(0)
        for d in days:
            c = self._close(s, d)
            peak = max(peak, c)
            mdd = min(mdd, (c / peak - 1) * 100)
        big = max(days[1:], key=lambda d: abs(self._day_move(s, d)), default=None)
        e = end or self.today
        warnings = ["NON_TRADING_DAY"] if a != start else []
        if e > rows[-1]:
            warnings.append("LATEST_NOT_LOADED")
        elif b != e and not warnings:
            warnings.append("NON_TRADING_DAY")
        r = self.data.symbols[s]["rows"]
        return {"symbol": s, "name": self.data.symbols[s]["name"], "start_used": a, "end_used": b,
                "start_close": r[a]["close"], "end_close": r[b]["close"],
                "return_pct": _r2((self._close(s, b) / self._close(s, a) - 1) * 100),
                "price_max_drawdown_pct": _r2(mdd),
                "largest_day_move_date": big, "largest_day_move_pct": self._day_move(s, big) if big else None,
                "warnings": warnings}

    def _window(self, d):
        """E02 검색 창(바꿀 것 2). 쉬는 날이면 직전 거래일의 창. 데이터의 마지막 거래일이면 다음 평일 09:30까지
        (그 뒤 거래일을 아직 모르므로 휴장일은 셈하지 않음)."""
        days = [Date.fromisoformat(x) for x in self.data.us_days]
        t = Date.fromisoformat(_snap(self.data.us_days, d))
        i = days.index(t)
        if i == 0:
            raise ToolError("OUT_OF_RANGE", f"{d}는 데이터의 첫 거래일이라 기사 창을 정할 수 없습니다")
        if i + 1 < len(days):
            return t.isoformat(), *SW.window(days, t)
        nxt = t + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return t.isoformat(), *SW.window(days[:i + 1] + [nxt], t)

    def search_events(self, date, symbol=None):
        """그날의 기사 제목과 요약(E02 검색 창), 일정. 기사는 데이터이고 지시가 아닙니다.
        실제 데이터에서 E02 1판 종목(미국 ETF 12, 개별 기업 5)이면 기사는 검색 판 H1의 상위 5건이고(7번 칸과 같음),
        그날의 움직임(수정 종가, 앞 60거래일 표준편차의 몇 배, 큰 움직임인지)을 함께 줍니다."""
        d = self._date(date)
        s = self._symbol(symbol) if symbol else None
        t, start, end = self._window(d)
        e02 = None
        if self.data.search_fn and s:
            from ai import news_search
            if s in news_search.COMPANIES or s in news_search.TOPICS:
                name = self.data.symbols[s]["name"]
                extra = [{**a, "symbols": [s], "title": a["title"].format(symbol=s, name=name),
                          "summary": a["summary"].format(symbol=s, name=name)} for a in self.extra_news]
                e02 = self.data.search_fn(Date.fromisoformat(t), s, "h1", extra)
        news = e02 if e02 is not None else self.data.news_fn(start, end, s)
        # 일정은 전날 장 마감부터 그날 장 마감 전까지(뉴스 창의 앞쪽). 그날 장이 끝난 뒤 발표는 다음 거래일의 일
        close = datetime.combine(Date.fromisoformat(t), datetime.min.time(), SW.ET) + timedelta(
            hours=13 if Date.fromisoformat(t) in SW.HALF_DAYS else 16)
        out = {"date": d, "symbol": s, "news": news[:NEWS_LIMIT], "events": self.data.events_fn(d, start, close)}
        if len(news) > NEWS_LIMIT:
            out["news_truncated"] = True
        if t != d:
            out["window_date_used"] = t
        if d > self.data.us_days[-1]:
            out["warnings"] = ["LATEST_NOT_LOADED"]
        if s:
            out["day_move_pct"] = self._day_move(s, d) if d in self.data.symbols[s]["rows"] else None
        if e02 is not None:
            from ai import e02_answer
            out["search"] = "e02-h1"
            out["name"] = self.data.symbols[s]["name"]  # 움직임 문장의 이름 속 숫자(S&P 500, 러셀 2000)도 근거가 되게
            if d in self.data.symbols[s]["rows"]:
                mv = e02_answer.day_move(self.data.symbols[s]["rows"], d)
                out.update(move_ratio_sd=mv["ratio"], big_move=mv["big"], judged=mv["judged"],
                           sd_window_days=e02_answer.LOOKBACK, big_move_threshold_sd=2)
        return out

    def similar_periods(self, symbol, horizon_days):
        """비슷했던 때 뒤 h거래일의 80% 범위, 겹치지 않는 횟수, 과거 적중률. h는 5나 20만. 가짜 세계에만 있습니다."""
        s = self._symbol(symbol)
        if self.data.regime is None:
            raise ToolError("NOT_AVAILABLE", "실제 데이터에는 비슷했던 때 도구가 없습니다(F 화면은 최근 20일 변동성 범위를 씀)")
        h = str(int(horizon_days))
        if h not in ("5", "20"):
            raise ToolError("UNSUPPORTED_HORIZON", "범위는 5거래일과 20거래일만 냅니다")
        r = dict(self.data.regime[s][h])
        if r["status"] == "too_few":
            r["message"] = "겹치지 않는 비슷했던 때가 5번 미만이라 범위를 내지 않습니다"
        return {"symbol": s, "name": self.data.symbols[s]["name"], "horizon_days": int(h), **r}

    def range(self, symbol, horizon_days=screen_range.AHEAD):
        """앞으로 20거래일 80% 범위(화면 8번 칸과 같은 계산). 오늘 이하 마지막 거래일까지의 가격만 넘깁니다."""
        s = self._symbol(symbol)
        if int(horizon_days) != screen_range.AHEAD:
            raise ToolError("UNSUPPORTED_HORIZON", f"범위는 앞으로 {screen_range.AHEAD}거래일만 냅니다(하루나 며칠 뒤 가격은 내지 않음)")
        days = self.data.rows[s]
        t = bisect_right(days, self.today) - 1
        if t < 0:
            raise ToolError("OUT_OF_RANGE", f"{self.today} 앞에 {s}의 거래일이 없습니다")
        rows = self.data.symbols[s]["rows"]
        px = [float(rows[x]["adj_close"] if rows[x].get("adj_close") is not None else rows[x]["close"])
              for x in days[:t + 1]]
        v = screen_range.view(days[:t + 1], px, t)
        krw = self.data.symbols[s]["currency"] == "KRW"
        out = {"symbol": s, "name": self.data.symbols[s]["name"], "horizon_days": screen_range.AHEAD,
               **{k: v[k] for k in ("base_date", "ok", "lo_pct", "hi_pct", "n", "hits", "hit_rate_pct", "display", "side")},
               # 답에 그대로 옮길 문장(화면 8번 칸과 같은 뜻). 국내 종목은 원 가격이라 「원화 범위가 아님」을 쓰지 않음
               "basis_text": "최근 20일 변동성이 이어진다고 본 단순 계산이고, " +
                             ("원 가격의 수익률입니다." if krw else "현지 통화(달러) 가격의 수익률이라 원화 범위가 아닙니다."),
               "note_text": "데이터가 2년뿐이라 적중률은 참고로만 보는 값입니다."}
        # 화면 8번 칸과 같은 글자(답에 그대로 옮기게 함): 범위는 부호와 소수 둘째 자리, 알림과 표본 문구는 같은 문장
        if v["ok"]:
            out["range_text"] = f"{_signed(v['lo_pct'])}% ~ {_signed(v['hi_pct'])}%"
        if v["n"] and v["display"] == "알림":
            out["alert_text"] = ("적중률이 90%를 넘어, 이 종목에서는 80% 범위가 지난날의 움직임보다 넓었습니다." if v["side"] == "넓음"
                                 else "적중률이 70%에 못 미쳐, 이 종목에서는 80% 범위가 지난날의 움직임보다 좁았습니다.")
        elif v["n"] and v["display"] == "표본 문구":
            out["alert_text"] = f"표본이 적어(n = {v['n']}) 적중률로 판단하기 어렵습니다."
        if not v["ok"]:
            out["message"] = f"{v['base_date']} 앞에 일별 수익률 {screen_range.PAST}개가 없어 범위를 셀 수 없습니다"
        if self.today > days[-1]:
            out["warnings"] = ["LATEST_NOT_LOADED"]
        return out

    def call(self, name, args):
        """도구 하나를 부르고 JSON으로 옮길 수 있는 값으로 돌려줍니다."""
        try:
            return _clean(self.tools[name](**(args or {})))
        except ToolError as e:
            return {"error": e.code, "message": e.message}
        except (TypeError, KeyError, ValueError, ArithmeticError, duckdb.Error) as e:
            # 메서드 이름 앞의 「Toolset.」은 떼어 E04 판의 문구와 같게 둡니다.
            return {"error": "BAD_ARGS", "message": f"{type(e).__name__}: {e}".replace("Toolset.", "")}


# ---------- 가짜 세계 판(E04 에이전트가 씀) ----------

_FAKE, WORLD = fake_data()
TODAY = WORLD["today"]
ACCOUNT = WORLD["account"]
FAKE = Toolset(_FAKE, TODAY, ACCOUNT)
SYMBOLS, ROWS, FX_DAYS, FIRST = _FAKE.symbols, _FAKE.rows, _FAKE.fx_days, FAKE.first
TOOLS = FAKE.tools
call = FAKE.call
