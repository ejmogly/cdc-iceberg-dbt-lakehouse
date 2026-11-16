"""손익 계산은 제품 SQL(sql/pnl/)을 그대로 씁니다. E04 정답을 만든 독립 계산기(eval/e01_pnl/make_expected.py)와 공유하지 않습니다.

시세, 환율, 달력을 DuckDB 메모리 테이블에 한 번 넣고, 부를 때마다 계산 단위와 매매 줄만 바꿔 모델을 조회합니다.
ref()를 뷰 이름으로, dbt.datediff를 date_diff로 바꾸는 것은 eval/e01_pnl/check_sql.py와 같습니다.
"""
import re
from bisect import bisect_right
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

SQL = Path(__file__).resolve().parents[1] / "sql" / "pnl"
INPUTS = {"pnl_calendar", "pnl_prices", "pnl_fx", "pnl_instruments", "pnl_positions", "pnl_trades"}
REF = re.compile(r"\{\{\s*ref\('([a-z0-9_]+)'\)\s*\}\}")
DATEDIFF = re.compile(r'\{\{\s*dbt\.datediff\("([^"]+)",\s*"([^"]+)",\s*"day"\)\s*\}\}')


def _models():
    src = {p.stem: p.read_text(encoding="utf-8") for p in SQL.glob("*.sql")}
    deps = {k: set(REF.findall(v)) - INPUTS for k, v in src.items()}
    order = []

    def visit(k):
        if k not in order:
            for d in sorted(deps[k]):
                visit(d)
            order.append(k)

    for k in sorted(src):
        visit(k)
    render = lambda t: REF.sub(lambda m: m.group(1),
                               DATEDIFF.sub(lambda m: f"date_diff('day', {m.group(1)}, {m.group(2)})", t))
    return [(k, render(src[k])) for k in order]


def _dec(v):
    return None if v is None else Decimal(str(v))


# ---------- 입력 검사(화면 서버와 도구가 함께 씀) ----------

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
# 손익 SQL 입력 칸: 수량 integer, 체결가 decimal(18, 4), 환율 decimal(12, 4).
_LIMITS = {"qty": (Decimal(2 ** 31 - 1), None), "price": (Decimal("99999999999999.9999"), 4),
           "fx": (Decimal("99999999.9999"), 4)}


def iso_date(s):
    """YYYY-MM-DD 꼴의 실제 날짜인지. date.fromisoformat은 20250304, 2025-W10-2도 받아들여, 날짜를 문자열로
    비교하는 곳(데이터 기간, 기준일)이 틀어집니다."""
    if not isinstance(s, str) or not _ISO.fullmatch(s):
        return False
    try:
        return date.fromisoformat(s).isoformat() == s
    except ValueError:
        return False


def number(v, kind):
    """매매 줄의 수(qty, price, fx)를 Decimal로. 손익 SQL 칸에 들어가지 않으면 ValueError(문구는 화면에 보임).
    수량의 소수와 0 이하는 여기서 막지 않습니다(FRACTIONAL_QTY, BAD_QTY 경고로 알림)."""
    if isinstance(v, bool):
        raise ValueError("수가 아닙니다")
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        raise ValueError("수가 아닙니다") from None
    if not d.is_finite():
        raise ValueError("수가 아닙니다")
    top, places = _LIMITS[kind]
    if abs(d) > top:
        raise ValueError("너무 큽니다")
    if places is not None:
        if d <= 0:
            raise ValueError("0보다 커야 합니다")
        if d != d.quantize(Decimal(1).scaleb(-places)):
            raise ValueError(f"소수 {places}자리까지입니다")
    return d


# 화면 4번 칸의 SPY 선(설계서 2절, 4절 「새로 짠 계산」). 제품 SQL(sql/pnl/)에는 넣지 않고 화면에서만 씁니다.
# pnl_position의 bench_total_krw와 같은 정의를 날마다 셉니다: 기준 종목을 판 돈(누적) + 남은 기준 종목 단위 x 그날
# 기준 종목 원화 값 - 매수 금액(누적). 매매 상태는 pnl_daily처럼 그날의 매매를 모두 처리한 뒤로 잡습니다.
BENCH_DAILY = """
with steps as (
  select
    r.position_id,
    r.calc_date,
    sum(r.invested_krw) over w as invested_cum,
    sum(case when r.is_sell then r.bench_flow_krw else 0 end) over w as bench_cash_cum,
    l.bench_units_after,
    row_number() over (partition by r.position_id, r.calc_date order by r.step desc) as rn
  from pnl_trade_result r
  join pnl_ledger l on l.position_id = r.position_id and l.step = r.step
  window w as (partition by r.position_id order by r.step rows between unbounded preceding and current row)
),
state as (
  select position_id, calc_date as valid_from,
         lead(calc_date) over (partition by position_id order by calc_date) as valid_to,
         invested_cum, bench_cash_cum, bench_units_after
  from steps where rn = 1
)
select
  s.position_id,
  c.cal_date,
  s.bench_cash_cum + cast(s.bench_units_after as double) * cast(m.close_filled * m.usdkrw as double)
    - cast(s.invested_cum as double) as bench_cum_krw
from state s
join pnl_position_base b on b.position_id = s.position_id
join pnl_calendar c
  on c.cal_date >= s.valid_from and (s.valid_to is null or c.cal_date < s.valid_to) and c.cal_date <= b.end_date
join pnl_market_daily m on m.symbol = b.bench_symbol and m.cal_date = c.cal_date
"""


class Engine:
    def __init__(self, world):
        con = duckdb.connect()
        con.execute("create table pnl_calendar (cal_date date)")
        con.executemany("insert into pnl_calendar values (?)", [[d] for d in world["calendar"]])
        con.execute("create table pnl_prices (symbol varchar, trade_date date, close_price decimal(18, 4), "
                    "low_price decimal(18, 4), high_price decimal(18, 4))")
        con.executemany("insert into pnl_prices values (?, ?, ?, ?, ?)",
                        [[s, d, _dec(r["close"]), _dec(r["low"]), _dec(r["high"])]
                         for s, v in world["symbols"].items() for d, r in v["rows"].items()])
        con.execute("create table pnl_fx (rate_date date, rate decimal(12, 4))")
        con.executemany("insert into pnl_fx values (?, ?)", [[d, _dec(v)] for d, v in world["usdkrw"].items()])
        con.execute("create table pnl_instruments (symbol varchar, currency varchar)")
        con.executemany("insert into pnl_instruments values (?, ?)",
                        [[s, v["currency"]] for s, v in world["symbols"].items()])
        con.execute("create table pnl_positions (position_id varchar, symbol varchar, asof_date date, "
                    "bench_symbol varchar)")
        con.execute("create table pnl_trades (position_id varchar, trade_seq integer, trade_date date, "
                    "side varchar, qty integer, price decimal(18, 4), fx_rate decimal(12, 4))")
        # 모델은 부를 때마다 순서대로 한 번씩 계산해 테이블로 둡니다(dbt의 table 구체화와 같음). 뷰로 두면
        # pnl_position, pnl_sells, pnl_daily를 읽을 때마다 앞 단계 전체를 다시 계산해 한 번에 0.5초를 넘었습니다.
        self.models = _models() + [("screen_bench_daily", BENCH_DAILY)]
        self.con = con

    def series(self):
        """run() 바로 뒤에 부릅니다. 화면 3, 4번 칸의 재료: 날마다 내 누적 손익과 고점, SPY 선(원, 반올림 전),
        매매 줄마다 처리 뒤 수량과 평균 단가. 줄 번호(trade_seq)는 run()에 넣은 순서입니다."""
        daily = self.con.execute(
            "select coalesce(d.cal_date, b.cal_date), d.pnl_cum_krw, d.peak_krw, b.bench_cum_krw "
            "from pnl_daily d full join screen_bench_daily b on b.position_id = d.position_id "
            "and b.cal_date = d.cal_date order by 1").fetchall()
        steps = self.con.execute(
            "select r.trade_seq, r.calc_date, r.side, r.qty, r.calc_price, r.calc_fx, r.oversold, r.qty_after, "
            "l.cost_local_after from pnl_trade_result r join pnl_ledger l "
            "on l.position_id = r.position_id and l.step = r.step order by r.step").fetchall()
        return daily, steps

    def run(self, symbol, trades, asof, bench="BENCH"):
        """trades: [{"d", "side", "q", "p"?, "fx"?}]. 돌려주는 것: 요약(pnl_position 칸), 매도, 경고."""
        con = self.con
        con.execute("delete from pnl_positions")
        con.execute("delete from pnl_trades")
        con.execute("insert into pnl_positions values ('x', ?, ?, ?)", [symbol, asof, bench])
        con.executemany("insert into pnl_trades values ('x', ?, ?, ?, ?, ?, ?)",
                        [[k, t["d"], t["side"], t["q"], _dec(t.get("p")), _dec(t.get("fx"))]
                         for k, t in enumerate(trades)])
        for name, sql in self.models:
            con.execute(f"create or replace table {name} as {sql}")
        cur = con.execute("select * exclude (position_id) from pnl_position")
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        summary = {c: v for c, v in zip(cols, row) if not c.startswith("gap_")} if row else {}
        sells = [{"date": d.isoformat(), "qty": q, "price": p, "avg_cost_before": a, "realized_krw": k}
                 for d, q, p, a, k in con.execute("select sell_date, qty, price, avg_cost, pnl_krw "
                                                  "from pnl_sells order by step").fetchall()]
        warnings = [{"code": c, "trade_index": s} for _, c, s in
                    con.execute("select * from pnl_warnings order by trade_seq, code").fetchall()]
        return summary, sells, warnings


def _last_on_or_before(days, d):
    i = bisect_right(days, d)
    return days[i - 1] if i else None


def compute(engine, world, symbol, trades, asof, bench):
    """화면과 도구가 함께 쓰는 손익(docs/ai/F-screen.md 4절 「입력 경고」). trades의 순서가 경고 번호입니다.

    손익 SQL 앞에서 두 경고로 줄을 빼고(OUT_OF_DATA_RANGE: 그 종목 시세의 첫날 앞이나 마지막 날 뒤,
    FRACTIONAL_QTY: 소수 수량), SQL 뒤에서 FX_CARRIED(계산에 쓰인 미국 종목 줄에 환율을 넣지 않았고, 달력으로
    맞춘 그날의 환율이 없음)를 붙입니다. 초과 매도, 기준일 뒤, 수량 0 이하로 SQL이 뺀 줄에는 FX_CARRIED를 붙이지 않습니다.
    빠진 두 경고의 줄에는 다른 경고를 붙이지 않습니다. 경고는 (줄 번호, 코드) 순서입니다.
    돌려주는 것: (요약, 매도, 경고, SQL에 넣은 줄의 원래 번호 목록). series()의 trade_seq는 이 목록의 자리입니다.
    """
    rows = world["symbols"][symbol]["rows"]
    first, last = min(rows), max(rows)
    pre, kept = [], []
    for k, t in enumerate(trades):
        if t["d"] < first or t["d"] > last:
            pre.append(("OUT_OF_DATA_RANGE", k))
            continue
        q = Decimal(str(t["q"]))
        if q != q.to_integral_value():
            pre.append(("FRACTIONAL_QTY", k))
            continue
        kept.append((k, {**t, "q": int(q)}))
    summary, sells, sql_warnings = engine.run(symbol, [u for _, u in kept], asof, bench) if kept else ({}, [], [])
    idx = [k for k, _ in kept]
    found = [(w["code"], idx[w["trade_index"]]) for w in sql_warnings]
    dropped = {k for c, k in found if c in ("OVERSOLD", "AFTER_ASOF", "BAD_QTY")}
    if world["symbols"][symbol]["currency"] == "USD":
        calendar = world["calendar"]
        for k, u in kept:
            if u.get("fx") is None and k not in dropped and _last_on_or_before(calendar, u["d"]) not in world["usdkrw"]:
                pre.append(("FX_CARRIED", k))
    warnings = [{"code": c, "trade_index": k} for c, k in sorted(pre + found, key=lambda w: (w[1], w[0]))]
    return summary, sells, warnings, idx
