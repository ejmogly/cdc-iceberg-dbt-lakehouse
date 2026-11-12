#!/usr/bin/env python3
"""E01 골든 사례 30개를 sql/pnl/의 손익 SQL에 넣어 DuckDB로 돌리고 정답(expected.json)과 맞춥니다.

사용: .venv/bin/python eval/e01_pnl/check_sql.py [--show G13 | --no-save]

sql/pnl/의 파일은 dbt 모델 모양({{ ref('...') }})이라, 여기서 이름만 바꿔 DuckDB 뷰로 만듭니다.
평가 기준(docs/ai/experiments/E01-pnl.md 3절)은 바꾸지 않습니다. 어긋나면 SQL을 먼저 의심합니다.
확인하는 것: 정답 칸 전부(반올림한 값), 연 환산 0.05%p 안, 항등식(반올림 전 0.01원 미만), 경고 코드와 줄 번호,
입력 순서를 섞어도 같은지, 두 번 돌려도 같은지. 결과는 sql_check_result.json(--no-save면 쓰지 않음, make check와 CI).
"""
import json
import random
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
SQL = ROOT / "sql" / "pnl"
FIX = json.loads((HERE / "fixture.json").read_text(encoding="utf-8"))
CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
SEED = 20261111
SHUFFLES = 5

INPUTS = {"pnl_calendar", "pnl_prices", "pnl_fx", "pnl_instruments", "pnl_positions", "pnl_trades"}
REF = re.compile(r"\{\{\s*ref\('([a-z0-9_]+)'\)\s*\}\}")
DATEDIFF = re.compile(r'\{\{\s*dbt\.datediff\("([^"]+)",\s*"([^"]+)",\s*"day"\)\s*\}\}')
EXACT = ["qty_left", "avg_cost_local", "avg_buy_fx", "realized_local", "unrealized_local", "total_local",
         "realized_krw", "unrealized_krw", "total_krw", "invested_krw", "simple_return_pct", "price_effect_krw",
         "fx_effect_krw", "cross_effect_krw", "bench_total_krw", "max_drawdown_krw"]
NEAR = ["irr_pct", "bench_irr_pct"]


def render(text):
    """dbt 문법 두 가지를 DuckDB로: ref()는 뷰 이름, dbt.datediff는 date_diff."""
    text = DATEDIFF.sub(lambda m: f"date_diff('day', {m.group(1)}, {m.group(2)})", text)
    return REF.sub(lambda m: m.group(1), text)


def models():
    """파일 이름이 모델 이름입니다. ref()를 따라 만들 순서를 정합니다."""
    src = {p.stem: p.read_text(encoding="utf-8") for p in SQL.glob("*.sql")}
    deps = {k: set(REF.findall(v)) - INPUTS for k, v in src.items()}
    order = []

    def visit(k):
        if k in order:
            return
        for d in sorted(deps[k]):
            visit(d)
        order.append(k)

    for k in sorted(src):
        visit(k)
    return [(k, render(src[k])) for k in order]


def snap(d):
    return max(c for c in FIX["calendar"] if c <= d)


def build(cases):
    con = duckdb.connect()
    con.execute("create table pnl_calendar (cal_date date)")
    con.executemany("insert into pnl_calendar values (?)", [[d] for d in FIX["calendar"]])
    con.execute("create table pnl_prices (symbol varchar, trade_date date, close_price decimal(18, 4), "
                "low_price decimal(18, 4), high_price decimal(18, 4))")
    con.executemany("insert into pnl_prices values (?, ?, ?, ?, ?)",
                    [[s, d, str(r["close"]), str(r["low"]), str(r["high"])]
                     for s, v in FIX["symbols"].items() for d, r in v["rows"].items()])
    con.execute("create table pnl_fx (rate_date date, rate decimal(12, 4))")
    con.executemany("insert into pnl_fx values (?, ?)", [[d, str(v)] for d, v in FIX["usdkrw"].items()])
    con.execute("create table pnl_instruments (symbol varchar, currency varchar)")
    con.executemany("insert into pnl_instruments values (?, ?)", [[s, v["currency"]] for s, v in FIX["symbols"].items()])
    con.execute("create table pnl_positions (position_id varchar, symbol varchar, asof_date date, bench_symbol varchar)")
    con.executemany("insert into pnl_positions values (?, ?, ?, ?)",
                    [[c["id"], c["symbol"], c["asof"], "BENCH"] for c in cases])
    con.execute("create table pnl_trades (position_id varchar, trade_seq integer, trade_date date, side varchar, "
                "qty integer, price decimal(18, 4), fx_rate decimal(12, 4))")
    con.executemany("insert into pnl_trades values (?, ?, ?, ?, ?, ?, ?)",
                    [[c["id"], k, t["d"], t["side"], t["q"], str(t["p"]) if "p" in t else None,
                      str(t["fx"]) if "fx" in t else None] for c in cases for k, t in enumerate(c["trades"])])
    for name, sql in models():
        con.execute(f"create view {name} as {sql}")
    return con


def run(cases):
    """사례 id마다 (요약 dict, 매도 목록, 경고 목록)."""
    con = build(cases)
    cols = [d[0] for d in con.execute("select * from pnl_position limit 0").description]
    out = {r[0]: {"summary": dict(zip(cols, r)), "sells": [], "warnings": []}
           for r in con.execute("select * from pnl_position").fetchall()}
    for pid, d, q, price, avg, pnl in con.execute(
            "select position_id, sell_date, qty, price, avg_cost, pnl_krw from pnl_sells order by position_id, step").fetchall():
        out[pid]["sells"].append((d.isoformat(), q, Decimal(price), Decimal(avg), Decimal(pnl)))
    for pid, code, seq in con.execute("select * from pnl_warnings").fetchall():
        out[pid]["warnings"].append((code, seq))
    return out


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return Decimal(str(a)) == Decimal(str(b))


def compare(case_id, got):
    exp = EXPECTED[case_id]
    s = got["summary"]
    bad = [f"{k}: 정답 {exp[k]}, SQL {s[k]}" for k in EXACT if not same(exp[k], s[k])]
    for k in NEAR:
        if (exp[k] is None) != (s[k] is None) or (exp[k] is not None and abs(Decimal(str(exp[k])) - s[k]) > Decimal("0.05")):
            bad.append(f"{k}: 정답 {exp[k]}, SQL {s[k]}")
    want = [(x["d"], x["q"], Decimal(str(x["price"])), Decimal(str(x["avg_cost"])), Decimal(x["pnl_krw"]))
            for x in exp["sells"]]
    if want != got["sells"]:
        bad.append(f"sells: 정답 {want}, SQL {got['sells']}")
    if sorted((w["code"], w["trade"]) for w in exp["warnings"]) != sorted(got["warnings"]):
        bad.append(f"warnings: 정답 {exp['warnings']}, SQL {got['warnings']}")
    gaps = {k: s[k] for k in ("gap_realized_unrealized", "gap_effects") if abs(s[k]) >= 0.01}
    return bad, gaps


def shuffled(case, rng):
    """날짜(달력에 맞춘 날)가 다른 매매끼리만 섞고, 같은 날 안의 순서는 유지합니다. (새 사례, 새 번호 → 원래 번호)."""
    n = len(case["trades"])
    slots = list(range(n))
    rng.shuffle(slots)
    groups = {}
    for k, t in enumerate(case["trades"]):
        groups.setdefault(snap(t["d"]), []).append(k)
    pos_of_group = {g: sorted(slots[k] for k in ks) for g, ks in groups.items()}
    new = [None] * n
    for g, ks in groups.items():
        for k, slot in zip(ks, pos_of_group[g]):
            new[slot] = k
    return {**case, "trades": [case["trades"][k] for k in new]}, new


def show(case_id):
    con = build([c for c in CASES if c["id"] == case_id])
    print(con.sql("select step, trade_seq, calc_date, side, qty, calc_price, calc_fx, qty_before, qty_after, "
                  "round(cost_krw_after, 2) as cost_krw_after, round(realized_krw, 2) as realized_krw, oversold "
                  "from pnl_trade_result order by step"))
    print(con.sql("select * exclude (position_id) from pnl_position"))
    print(con.sql("select * from pnl_warnings"))


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--show":
        show(sys.argv[2])
        return 0
    first = run(CASES)
    second = run(CASES)
    rng = random.Random(SEED)
    rows, fails, gap_fails, naive_ok, order_fail = [], 0, 0, 0, []
    for c in CASES:
        bad, gaps = compare(c["id"], first[c["id"]])
        fails += bool(bad)
        gap_fails += bool(gaps)
        naive_ok += same(EXPECTED[c["id"]]["naive_krw"], first[c["id"]]["summary"]["naive_krw"])
        rows.append({"id": c["id"], "pass": not bad and not gaps, "mismatch": bad, "identity_gaps": gaps})
    for c in CASES:
        for _ in range(SHUFFLES):
            sc, mapping = shuffled(c, rng)
            got = run([sc])[c["id"]]
            got["warnings"] = [(code, mapping[seq]) for code, seq in got["warnings"]]
            bad, _ = compare(c["id"], got)
            if bad:
                order_fail.append({"id": c["id"], "order": mapping, "mismatch": bad})
                break
    rerun_same = all(first[k]["summary"] == second[k]["summary"] and first[k]["sells"] == second[k]["sells"]
                     and sorted(first[k]["warnings"]) == sorted(second[k]["warnings"]) for k in first)

    print("| 사례 | 결과 | 어긋난 칸 |")
    print("|---|---|---|")
    for r in rows:
        print(f"| {r['id']} | {'통과' if r['pass'] else '실패'} | "
              f"{'; '.join(r['mismatch'] + [f'항등식 {k} {v}' for k, v in r['identity_gaps'].items()])} |")
    max_gap = max(max(abs(first[k]["summary"]["gap_realized_unrealized"]), abs(first[k]["summary"]["gap_effects"]))
                  for k in first)
    summary = {
        "checked": date.today().isoformat(),
        "engine": f"duckdb {duckdb.__version__}",
        "cases": len(CASES),
        "numbers_and_warnings_pass": len(CASES) - fails,
        "identity_pass": len(CASES) - gap_fails,
        "identity_max_gap_krw": max_gap,
        "order_shuffle": {"tries_per_case": SHUFFLES, "seed": SEED, "fail": order_fail},
        "rerun_same": rerun_same,
        "baseline_naive_krw_same": naive_ok,
    }
    print(f"\n숫자와 경고 일치 {summary['numbers_and_warnings_pass']}/{len(CASES)}, "
          f"항등식 {summary['identity_pass']}/{len(CASES)}(가장 큰 차이 {max_gap:.2e}원), "
          f"입력 순서 섞기 {len(CASES) - len(order_fail)}/{len(CASES)}(사례마다 {SHUFFLES}번), "
          f"다시 실행 {'같음' if rerun_same else '다름'}, 기준선 칸 일치 {naive_ok}/{len(CASES)}")
    if "--no-save" not in sys.argv:
        (HERE / "sql_check_result.json").write_text(
            json.dumps({**summary, "rows": rows}, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    ok = fails == 0 and gap_fails == 0 and not order_fail and rerun_same
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
