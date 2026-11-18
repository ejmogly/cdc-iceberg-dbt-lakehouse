#!/usr/bin/env python3
"""E01 골든 사례 30개를 dbt/models/pnl/의 손익 모델(Spark 판)에 넣어 레이크하우스의 Spark로 돌리고 정답과 맞춥니다.

사용(저장소 루트에서, Spark 컨테이너가 떠 있어야 함): .venv/bin/python eval/e01_pnl/check_spark.py [--no-save]

- 손익 모델은 sql/pnl/(DuckDB로 검증, check_sql.py)과 같은 파일이고, 장부(pnl_ledger)만 Spark 판입니다(재귀 CTE 대신
  aggregate 접기). 이 스크립트는 먼저 장부 밖 열 개가 sql/pnl/과 글자째 같은지 봅니다.
- 골든 입력(eval/e01_pnl/fixture.json, cases.json, 가짜 시세)을 Iceberg 이름공간 lake.e01에 테이블로 넣고, 모델을 ref() 순서대로
  CREATE OR REPLACE TABLE ... AS로 만든 뒤 pnl_position, pnl_sells, pnl_warnings를 읽어 check_sql.py와 같은 기준으로 맞춥니다
  (정답 칸 16개 정확히, 연 환산 0.05%p 안, 매도 한 건씩, 경고 코드와 줄 번호, 항등식 0.01원 미만).
- 입력 순서 섞기도 check_sql.py와 같은 시드와 순서로 사례마다 5번(150개) 함께 넣어, 원래 사례의 정답과 맞춥니다.
- 평가 기준과 정답은 바꾸지 않습니다. 결과는 eval/e01_pnl/spark_check_result.json(--no-save면 쓰지 않음).
"""
import json
import random
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import jinja2

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import check_sql as cs  # noqa: E402  같은 정답, 같은 비교

MODELS = ROOT / "dbt" / "models" / "pnl"
NS = "lake.e01"
OUT = HERE / "spark_check_result.json"


def same_files():
    """장부 밖 모델 열 개가 sql/pnl/과 같은지. 다른 파일 이름 목록."""
    return [p.name for p in sorted(cs.SQL.glob("*.sql"))
            if p.name != "pnl_ledger.sql" and (MODELS / p.name).read_text(encoding="utf-8") != p.read_text(encoding="utf-8")]


def render(text):
    """dbt 문법을 Spark SQL로: ref()는 lake.e01.<모델>, dbt.datediff(a, b, 'day')는 datediff(b, a), config()는 지움."""
    env = jinja2.Environment()
    dbt = type("Dbt", (), {"datediff": staticmethod(lambda a, b, part: f"datediff({b}, {a})")})()
    return env.from_string(text).render(ref=lambda n: f"{NS}.{n}", dbt=dbt, config=lambda **k: "")


def models():
    src = {p.stem: p.read_text(encoding="utf-8") for p in MODELS.glob("*.sql")}
    deps = {k: set(cs.REF.findall(v)) - cs.INPUTS for k, v in src.items()}
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


def lit(v, kind):
    if v is None:
        return "null"
    if kind == "date":
        return f"date '{v}'"
    if kind.startswith("decimal"):
        return f"cast('{v}' as {kind})"
    if kind == "int":
        return str(int(v))
    return "'" + str(v).replace("'", "''") + "'"


def table(name, cols, rows):
    """lake.e01에 입력 테이블 하나를 새로 만들어 값을 넣는 문장들."""
    spec = ", ".join(f"{c} {'string' if k == 'str' else k}" for c, k in cols)
    out = [f"drop table if exists {NS}.{name}", f"create table {NS}.{name} ({spec}) using iceberg"]
    if rows:
        vals = ",\n".join("(" + ", ".join(lit(v, k) for v, (_, k) in zip(r, cols)) + ")" for r in rows)
        out.append(f"insert into {NS}.{name} values\n{vals}")
    return out


def inputs(cases):
    fx = cs.FIX
    d4 = "decimal(18,4)"
    stmts = [f"create namespace if not exists {NS}"]
    stmts += table("pnl_calendar", [("cal_date", "date")], [[d] for d in fx["calendar"]])
    stmts += table("pnl_prices", [("symbol", "str"), ("trade_date", "date"), ("close_price", d4), ("low_price", d4),
                                  ("high_price", d4)],
                   [[s, d, r["close"], r["low"], r["high"]] for s, v in fx["symbols"].items() for d, r in v["rows"].items()])
    stmts += table("pnl_fx", [("rate_date", "date"), ("rate", "decimal(12,4)")], [[d, v] for d, v in fx["usdkrw"].items()])
    stmts += table("pnl_instruments", [("symbol", "str"), ("currency", "str")],
                   [[s, v["currency"]] for s, v in fx["symbols"].items()])
    stmts += table("pnl_positions", [("position_id", "str"), ("symbol", "str"), ("asof_date", "date"), ("bench_symbol", "str")],
                   [[c["id"], c["symbol"], c["asof"], "BENCH"] for c in cases])
    stmts += table("pnl_trades", [("position_id", "str"), ("trade_seq", "int"), ("trade_date", "date"), ("side", "str"),
                                  ("qty", "int"), ("price", d4), ("fx_rate", "decimal(12,4)")],
                   [[c["id"], k, t["d"], t["side"], t["q"], t.get("p"), t.get("fx")] for c in cases for k, t in enumerate(c["trades"])])
    return stmts


def spark_file(stmts):
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write(";\n\n".join(stmts) + ";\n")
        path = f.name
    p = subprocess.run([str(ROOT / "infra" / "spark" / "sql.sh"), path], cwd=ROOT, capture_output=True, text=True)
    Path(path).unlink()
    if p.returncode != 0:
        sys.exit("Spark에서 실패했습니다:\n" + (p.stdout + p.stderr)[-3000:])


def query(sql):
    import os
    p = subprocess.run([str(ROOT / "infra" / "spark" / "sql.sh"), "-e", sql], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "SQL_FORMAT": "tsv2"})
    if p.returncode != 0:
        sys.exit("Spark 조회 실패:\n" + p.stderr[-2000:])
    lines = [ln.split("\t") for ln in p.stdout.splitlines() if ln.strip()]
    head = [h.split(".")[-1] for h in lines[0]]
    return [dict(zip(head, [None if x in ("NULL", "") else x for x in r])) for r in lines[1:]]


def num(v):
    return None if v is None else Decimal(v)


def main():
    save = "--no-save" not in sys.argv
    diff_files = same_files()
    rng = random.Random(cs.SEED)
    copies = []  # (섞은 사본 id, 원래 사례 id, 새 번호 → 원래 번호)
    for c in cs.CASES:
        for i in range(cs.SHUFFLES):
            sc, mapping = cs.shuffled(c, rng)
            copies.append(({**sc, "id": f"{c['id']}_s{i + 1}"}, c["id"], mapping))
    t0 = time.time()
    spark_file(inputs(cs.CASES + [sc for sc, _, _ in copies])
               + [f"create or replace table {NS}.{k} using iceberg as\n{sql}" for k, sql in models()])
    built = time.time() - t0
    got = {}
    for r in query(f"select * from {NS}.pnl_position"):
        got[r["position_id"]] = {"summary": {k: (num(v) if k not in ("position_id", "currency") else v) for k, v in r.items()},
                                 "sells": [], "warnings": []}
    for r in query(f"select position_id, sell_date, qty, price, avg_cost, pnl_krw, step from {NS}.pnl_sells"):
        got[r["position_id"]]["sells"].append((int(r["step"]), (r["sell_date"], int(r["qty"]), Decimal(r["price"]),
                                                                Decimal(r["avg_cost"]), Decimal(r["pnl_krw"]))))
    for r in query(f"select position_id, code, trade_seq from {NS}.pnl_warnings"):
        got[r["position_id"]]["warnings"].append((r["code"], int(r["trade_seq"])))
    for g in got.values():
        g["sells"] = [x for _, x in sorted(g["sells"])]
    results, ok = [], 0
    for c in cs.CASES:
        if c["id"] not in got:
            results.append({"id": c["id"], "ok": False, "bad": ["결과 없음"]})
            continue
        bad, gaps = cs.compare(c["id"], got[c["id"]])
        bad += [f"항등식 {k} {v}" for k, v in gaps.items()]
        ok += not bad
        results.append({"id": c["id"], "ok": not bad, "bad": bad})
    order_fail = []
    for sc, orig, mapping in copies:
        g = got.get(sc["id"])
        if g is None:
            order_fail.append(sc["id"])
            continue
        g = {**g, "warnings": [(code, mapping[seq]) for code, seq in g["warnings"]]}
        bad, _ = cs.compare(orig, g)
        if bad:
            order_fail.append(sc["id"])
    for r in results:
        if not r["ok"]:
            print(f"  다름  {r['id']}: " + "; ".join(r["bad"])[:400])
    print(f"Spark 손익 모델: 골든 {ok}/{len(cs.CASES)}(정답 칸, 연 환산, 매도, 경고, 항등식), "
          f"입력 순서 섞기 {len(copies) - len(order_fail)}/{len(copies)}, "
          f"sql/pnl과 다른 파일 {diff_files or '없음(장부만 Spark 판)'}, 입력과 모델 만들기 {built:.1f}초")
    if save:
        OUT.write_text(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pass": ok, "cases": len(cs.CASES),
                                   "files_differ": diff_files, "build_seconds": round(built, 1), "results": results,
                                   "order_shuffle": {"copies": len(copies), "seed": cs.SEED, "fail": order_fail}},
                                  ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if ok == len(cs.CASES) and not diff_files and not order_fail else 1


if __name__ == "__main__":
    sys.exit(main())
