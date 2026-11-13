#!/usr/bin/env python3
"""E01 본 실행: sql/pnl/의 손익 모델을 고치지 않고 dbt(DuckDB 어댑터)로 빌드하고, 골든 사례를 dbt 테스트로 맞춥니다.

사용: .venv/bin/python eval/e01_pnl/run_dbt.py [--scale] [--no-save]

기본(골든): 사례 30개와 사례마다 섞은 사본 5개(check_sql.py와 같은 시드와 순서)를 넣고 dbt build를 두 번 돌립니다.
  테스트 e01_numbers(정답 칸, 연 환산), e01_sells, e01_warnings, e01_identity와 키 테스트, 두 번의 결과가 같은지를 봅니다.
--scale: 합성 계좌 1,000개 x 5종목 x 20건(체결 10만 건, 1년 달력)을 넣고 dbt build 한 번의 시간과
  계산 단위 하나를 고르는 조회의 p95를 잽니다. 정답이 없어 골든 테스트는 빼고 항등식과 키 테스트만 돕니다.
  합성 값은 시드로만 남기고 저장소에 두지 않습니다(DuckDB 파일은 dbt/target/, git 제외).
평가 기준(docs/ai/experiments/E01-pnl.md 3절)은 바꾸지 않습니다. 결과는 dbt_golden_result.json, dbt_scale_result.json.
"""
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from importlib import metadata
from pathlib import Path

import duckdb

import check_sql as cs

HERE = Path(__file__).parent
PROJECT = HERE / "dbt"
TARGET = PROJECT / "target"
DBT = Path(sys.executable).parent / "dbt"
GOLDEN_TESTS = ["e01_numbers", "e01_sells", "e01_warnings"]
SCALE_SEED = 20261112
SCALE_ACCOUNTS = 1000
SCALE_SYMBOLS = {"U1": "USD", "U2": "USD", "U3": "USD", "K1": "KRW", "K2": "KRW"}
SCALE_TRADES = 20
QUERY_TRIES = 200

DDL = {
    "in_calendar": "cal_date date",
    "in_prices": "symbol varchar, trade_date date, close_price decimal(18, 4), low_price decimal(18, 4), "
                 "high_price decimal(18, 4)",
    "in_fx": "rate_date date, rate decimal(12, 4)",
    "in_instruments": "symbol varchar, currency varchar",
    "in_positions": "position_id varchar, symbol varchar, asof_date date, bench_symbol varchar",
    "in_trades": "position_id varchar, trade_seq integer, trade_date date, side varchar, qty integer, "
                 "price decimal(18, 4), fx_rate decimal(12, 4)",
    "in_case_map": "position_id varchar, case_id varchar",
    "in_seq_map": "position_id varchar, trade_seq integer, orig_seq integer",
    "exp_position": "case_id varchar, " + ", ".join(f"{c} decimal(38, 8)" for c in cs.EXACT + cs.NEAR),
    "exp_sells": "case_id varchar, n integer, sell_date date, qty integer, price decimal(18, 4), "
                 "avg_cost decimal(18, 4), pnl_krw decimal(38, 0)",
    "exp_warnings": "case_id varchar, code varchar, trade_seq integer",
}


def num(v):
    return None if v is None else Decimal(str(v))


def fresh_db(name):
    TARGET.mkdir(exist_ok=True)
    db = TARGET / name
    for p in (db, Path(f"{db}.wal")):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(db))
    for t, cols in DDL.items():
        con.execute(f"create table {t} ({cols})")
    return db, con


def golden_db():
    """사례 30개(번호 그대로)와 섞은 사본 150개(G01_s1 등). 사본의 줄 번호는 in_seq_map으로 원래 번호에 잇습니다."""
    db, con = fresh_db("e01_golden.duckdb")
    rng = random.Random(cs.SEED)
    units = [(c["id"], c, list(range(len(c["trades"])))) for c in cs.CASES]
    for c in cs.CASES:
        for j in range(1, cs.SHUFFLES + 1):
            sc, mapping = cs.shuffled(c, rng)
            units.append((f"{c['id']}_s{j}", sc, mapping))
    fix = cs.FIX
    con.executemany("insert into in_calendar values (?)", [[d] for d in fix["calendar"]])
    con.executemany("insert into in_prices values (?, ?, ?, ?, ?)",
                    [[s, d, num(r["close"]), num(r["low"]), num(r["high"])]
                     for s, v in fix["symbols"].items() for d, r in v["rows"].items()])
    con.executemany("insert into in_fx values (?, ?)", [[d, num(v)] for d, v in fix["usdkrw"].items()])
    con.executemany("insert into in_instruments values (?, ?)", [[s, v["currency"]] for s, v in fix["symbols"].items()])
    con.executemany("insert into in_positions values (?, ?, ?, ?)",
                    [[pid, c["symbol"], c["asof"], "BENCH"] for pid, c, _ in units])
    con.executemany("insert into in_trades values (?, ?, ?, ?, ?, ?, ?)",
                    [[pid, k, t["d"], t["side"], t["q"], num(t.get("p")), num(t.get("fx"))]
                     for pid, c, _ in units for k, t in enumerate(c["trades"])])
    con.executemany("insert into in_case_map values (?, ?)", [[pid, c["id"]] for pid, c, _ in units])
    con.executemany("insert into in_seq_map values (?, ?, ?)",
                    [[pid, k, orig] for pid, _, mapping in units for k, orig in enumerate(mapping)])
    con.executemany(f"insert into exp_position values ({', '.join('?' * (1 + len(cs.EXACT + cs.NEAR)))})",
                    [[cid] + [num(e[k]) for k in cs.EXACT + cs.NEAR] for cid, e in cs.EXPECTED.items()])
    con.executemany("insert into exp_sells values (?, ?, ?, ?, ?, ?, ?)",
                    [[cid, n, x["d"], x["q"], num(x["price"]), num(x["avg_cost"]), num(x["pnl_krw"])]
                     for cid, e in cs.EXPECTED.items() for n, x in enumerate(e["sells"], 1)])
    con.executemany("insert into exp_warnings values (?, ?, ?)",
                    [[cid, w["code"], w["trade"]] for cid, e in cs.EXPECTED.items() for w in e["warnings"]])
    con.close()
    return db, len(units)


def scale_db():
    """합성 계좌 1,000개 x 5종목. 시세와 환율은 시드로 만든 걸음, 매매는 보유 안에서만 팔아 경고가 섞이지 않게 합니다."""
    db, con = fresh_db("e01_scale.duckdb")
    rng = random.Random(SCALE_SEED)
    days = [date(2025, 1, 1) + timedelta(n) for n in range(365)]
    days = [d for d in days if d.weekday() < 5]
    symbols = {**SCALE_SYMBOLS, "BENCH": "USD"}
    prices = {}
    for s, cur in symbols.items():
        close, places = (100.0, 2) if cur == "USD" else (50000.0, 0)
        for d in days:
            close *= math.exp(rng.gauss(0, 0.015))
            c = round(close, places)
            lo = min(c, round(c * (1 - rng.uniform(0.001, 0.02)), places))
            hi = max(c, round(c * (1 + rng.uniform(0.001, 0.02)), places))
            prices[s, d] = (c, lo, hi, places)
    fx, rate = [], 1380.0
    for d in days:
        rate = min(1450.0, max(1300.0, rate + rng.gauss(0, 4)))
        fx.append((d, round(rate, 2)))
    trades = TARGET / "scale_trades.csv"
    positions = []
    with trades.open("w", encoding="utf-8") as f:
        f.write("position_id,trade_seq,trade_date,side,qty,price,fx_rate\n")
        for a in range(1, SCALE_ACCOUNTS + 1):
            for s in SCALE_SYMBOLS:
                pid = f"A{a:04d}_{s}"
                positions.append([pid, s, days[-1], "BENCH"])
                held = 0
                for k, d in enumerate(sorted(rng.sample(days[:-1], SCALE_TRADES))):
                    if held == 0 or rng.random() < 0.6:
                        side, q = "BUY", rng.randint(1, 20)
                        held += q
                    else:
                        side, q = "SELL", rng.randint(1, held)
                        held -= q
                    c, lo, hi, places = prices[s, d]
                    price = round(rng.uniform(lo, hi), places) if rng.random() < 0.2 else ""
                    f.write(f"{pid},{k},{d},{side},{q},{price},\n")
    con.executemany("insert into in_calendar values (?)", [[d] for d in days])
    con.executemany("insert into in_prices values (?, ?, ?, ?, ?)",
                    [[s, d, num(c), num(lo), num(hi)] for (s, d), (c, lo, hi, _) in prices.items()])
    con.executemany("insert into in_fx values (?, ?)", [[d, num(r)] for d, r in fx])
    con.executemany("insert into in_instruments values (?, ?)", [[s, cur] for s, cur in symbols.items()])
    con.executemany("insert into in_positions values (?, ?, ?, ?)", positions)
    con.execute(f"copy in_trades from '{trades}' (header)")
    n_trades = con.execute("select count(*) from in_trades").fetchone()[0]
    con.close()
    trades.unlink()
    return db, len(positions), n_trades


def sync_models():
    """sql/pnl/*.sql을 dbt/models/pnl/로 복사합니다. 원본은 고치지 않고, 지난 복사본은 지웁니다."""
    dst = PROJECT / "models" / "pnl"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir()
    for p in cs.SQL.glob("*.sql"):
        shutil.copy2(p, dst / p.name)


def dbt_build(db, *args):
    sync_models()
    env = {**os.environ, "E01_DUCKDB": str(db), "DBT_PROFILES_DIR": str(PROJECT),
           "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    t0 = time.perf_counter()
    p = subprocess.run([str(DBT), "build", "--project-dir", str(PROJECT), *args],
                       env=env, capture_output=True, text=True)
    sec = time.perf_counter() - t0
    res = json.loads((TARGET / "run_results.json").read_text(encoding="utf-8"))["results"]
    if p.returncode not in (0, 1):
        print(p.stdout[-3000:], p.stderr[-3000:])
    nodes = [{"id": r["unique_id"].split(".")[-1] if r["unique_id"].startswith("model.") else r["unique_id"].split(".")[2],
              "kind": r["unique_id"].split(".")[0], "status": r["status"], "failures": r.get("failures"),
              "seconds": round(r["execution_time"], 3)} for r in res]
    return p.returncode, round(sec, 1), nodes


def snapshot(db):
    con = duckdb.connect(str(db), read_only=True)
    out = {t: con.execute(f"select * from {t} order by all").fetchall()
           for t in ("pnl_position", "pnl_sells", "pnl_warnings")}
    con.close()
    return out


def failed_units(db):
    """골든 테스트가 돌려준 행의 계산 단위(store_failures 테이블에서)."""
    con = duckdb.connect(str(db), read_only=True)
    out = {}
    for t in GOLDEN_TESTS + ["e01_identity"]:
        try:
            out[t] = {r[0] for r in con.execute(f"select position_id from main_dbt_test__audit.{t}").fetchall()}
        except duckdb.CatalogException:
            out[t] = set()
    con.close()
    return out


def versions():
    return {"dbt-core": metadata.version("dbt-core"), "dbt-duckdb": metadata.version("dbt-duckdb"),
            "duckdb": duckdb.__version__}


def golden(save):
    db, n_units = golden_db()
    code1, sec1, nodes = dbt_build(db)
    first, failed = snapshot(db), failed_units(db)
    code2, sec2, _ = dbt_build(db)
    rerun_same = snapshot(db) == first
    tests = [n for n in nodes if n["kind"] == "test"]
    models = [n for n in nodes if n["kind"] == "model"]
    ids = [c["id"] for c in cs.CASES]
    bad_any = set().union(*(failed[t] for t in GOLDEN_TESTS))
    numbers_pass = sum(i not in bad_any for i in ids)
    identity_pass = sum(i not in failed["e01_identity"] for i in ids)
    shuffle_fail = sorted({u.split("_s")[0] for u in bad_any | failed["e01_identity"] if "_s" in u})

    print("| 테스트 | 결과 | 돌려준 행 |")
    print("|---|---|---|")
    for t in tests:
        print(f"| {t['id']} | {t['status']} | {t['failures']} |")
    print(f"\n모델 {sum(m['status'] == 'success' for m in models)}/{len(models)}개 빌드, 계산 단위 {n_units}개"
          f"(사례 {len(ids)}, 섞은 사본 {n_units - len(ids)})")
    print(f"숫자, 매도, 경고 일치 {numbers_pass}/{len(ids)}, 항등식 {identity_pass}/{len(ids)}, "
          f"입력 순서 섞기 {len(ids) - len(shuffle_fail)}/{len(ids)}(사례마다 {cs.SHUFFLES}번), "
          f"다시 실행 {'같음' if rerun_same else '다름'}, dbt build {sec1}초와 {sec2}초")
    ok = (code1 == 0 and code2 == 0 and all(t["status"] == "pass" for t in tests)
          and numbers_pass == len(ids) and identity_pass == len(ids) and not shuffle_fail and rerun_same)
    if save:
        result = {"checked": date.today().isoformat(), "mode": "golden", **versions(), "cases": len(ids),
                  "units": n_units, "numbers_sells_warnings_pass": numbers_pass, "identity_pass": identity_pass,
                  "order_shuffle": {"tries_per_case": cs.SHUFFLES, "seed": cs.SEED, "fail": shuffle_fail},
                  "rerun_same": rerun_same, "build_seconds": [sec1, sec2], "pass": ok, "tests": tests,
                  "models": models}
        (HERE / "dbt_golden_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if ok else 1


def scale(save):
    db, n_units, n_trades = scale_db()
    code, sec, nodes = dbt_build(db, "--exclude", "tag:golden")
    tests = [n for n in nodes if n["kind"] == "test"]
    models = sorted((n for n in nodes if n["kind"] == "model"), key=lambda n: -n["seconds"])
    con = duckdb.connect(str(db), read_only=True)
    warnings = dict(con.execute("select code, count(*) from pnl_warnings group by 1 order by 1").fetchall())
    rng = random.Random(SCALE_SEED)
    picks = [r[0] for r in con.execute("select position_id from pnl_position order by 1").fetchall()]
    picks = rng.sample(picks, QUERY_TRIES)
    for pid in picks[:5]:
        con.execute("select * from pnl_position where position_id = ?", [pid]).fetchall()
    ms = []
    for pid in picks:
        t0 = time.perf_counter()
        con.execute("select * from pnl_position where position_id = ?", [pid]).fetchall()
        ms.append((time.perf_counter() - t0) * 1000)
    con.close()
    ms.sort()
    p50, p95 = round(ms[len(ms) // 2], 2), round(ms[math.ceil(len(ms) * 0.95) - 1], 2)

    print("| 테스트 | 결과 | 돌려준 행 |")
    print("|---|---|---|")
    for t in tests:
        print(f"| {t['id']} | {t['status']} | {t['failures']} |")
    print("\n| 오래 걸린 모델 | 초 |")
    print("|---|---|")
    for m in models[:5]:
        print(f"| {m['id']} | {m['seconds']} |")
    print(f"\n계산 단위 {n_units}개, 체결 {n_trades}건, dbt build {sec}초(통과선 300초), "
          f"조회 p50 {p50}ms, p95 {p95}ms({QUERY_TRIES}번, 화면 없이 DuckDB로), 경고 {warnings or '없음'}")
    ok = code == 0 and all(t["status"] == "pass" for t in tests)
    if save:
        result = {"checked": date.today().isoformat(), "mode": "scale", **versions(), "seed": SCALE_SEED,
                  "accounts": SCALE_ACCOUNTS, "units": n_units, "trades": n_trades, "build_seconds": sec,
                  "query_ms": {"tries": QUERY_TRIES, "p50": p50, "p95": p95}, "warnings": warnings,
                  "pass": ok, "tests": tests, "models": models}
        (HERE / "dbt_scale_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    save = "--no-save" not in sys.argv
    sys.exit(scale(save) if "--scale" in sys.argv else golden(save))
