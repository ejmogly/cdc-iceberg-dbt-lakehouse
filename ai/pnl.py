"""손익 계산은 제품 SQL(sql/pnl/)을 그대로 씁니다. E04 정답을 만든 독립 계산기(eval/e01_pnl/make_expected.py)와 공유하지 않습니다.

시세, 환율, 달력을 DuckDB 메모리 테이블에 한 번 넣고, 부를 때마다 계산 단위와 매매 줄만 바꿔 모델을 조회합니다.
ref()를 뷰 이름으로, dbt.datediff를 date_diff로 바꾸는 것은 eval/e01_pnl/check_sql.py와 같습니다.
"""
import re
from decimal import Decimal
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
        for name, sql in _models():
            con.execute(f"create view {name} as {sql}")
        self.con = con

    def run(self, symbol, trades, asof, bench="BENCH"):
        """trades: [{"d", "side", "q", "p"?, "fx"?}]. 돌려주는 것: 요약(pnl_position 칸), 매도, 경고."""
        con = self.con
        con.execute("delete from pnl_positions")
        con.execute("delete from pnl_trades")
        con.execute("insert into pnl_positions values ('x', ?, ?, ?)", [symbol, asof, bench])
        con.executemany("insert into pnl_trades values ('x', ?, ?, ?, ?, ?, ?)",
                        [[k, t["d"], t["side"], t["q"], _dec(t.get("p")), _dec(t.get("fx"))]
                         for k, t in enumerate(trades)])
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
