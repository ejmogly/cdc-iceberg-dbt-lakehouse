#!/usr/bin/env python3
"""규모 시험용 합성 매매를 원천 PostgreSQL에 넣고 지웁니다(docs/reports/2026-11-18_scale-test.md).

사용(저장소 루트에서, 원천 DB가 떠 있어야 함):
  .venv/bin/python loaders/load_test.py steady --name s20 --rate 20 --seconds 180
                                           초당 rate건, 건마다 한 트랜잭션(주문, 체결, 잔고)
  .venv/bin/python loaders/load_test.py burst --name b10k --count 10000 --batch 500
                                           count건을 batch건씩 한 트랜잭션으로 몰아서
  .venv/bin/python loaders/load_test.py clean --name clean --batch 5000
                                           A-LOAD 행을 모두 지움(체결 → 주문 → 잔고, batch건씩)
  .venv/bin/python loaders/load_test.py status
  .venv/bin/python loaders/load_test.py --selftest   DB 없이 매매 만들기 규칙 시험

규칙
- 계좌는 A-LOAD 하나입니다. 주문 번호는 800,000,000부터, 체결 번호는 주문 번호 + 50,000,000입니다.
  화면 매매(A-SCREEN, 900,000,000대)와 시나리오(작은 번호)와 겹치지 않습니다.
- 매매 한 건 = 주문(시장가, 전량 체결) + 체결 한 건 + 그 종목 잔고 더하기(한 트랜잭션). 모두 매수입니다.
  그래서 dbt의 잔고 대사, 주문 체결 수량 대사가 그대로 맞고, 손익 장부에 초과 매도가 생기지 않습니다.
- 체결가는 원천 daily_prices의 그날 종가, 미국 종목의 환율은 그날(없으면 직전) 원/달러 기준율입니다.
  날짜는 그 종목의 시세가 있는 거래일에서 고르고, 시각은 그 시장의 장 마감입니다(주말 거래일 없음).
- 명령마다 시작 전 WAL 위치, 끝난 뒤 WAL 위치, 시각, 건수를 data/generated/scale/runs.jsonl(git 제외)에 한 줄 남깁니다.
  측정 스크립트(eval/scale/measure.py)가 이 범위의 CDC 이벤트만 셉니다.
- 가격 값은 출력하지 않습니다(행 수와 시간만).
"""
import argparse
import json
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, time as dtime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "loaders"))

ACCOUNT = "A-LOAD"
ORDER_BASE, EXEC_OFFSET = 800_000_000, 50_000_000
RUNS = ROOT / "data" / "generated" / "scale" / "runs.jsonl"
CLOSE = {"KRX": (ZoneInfo("Asia/Seoul"), dtime(15, 30)), "US": (ZoneInfo("America/New_York"), dtime(16, 0))}
Q4 = Decimal("0.0001")


def instruments():
    import csv
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8", newline="") as f:
        return {r["symbol"]: r for r in csv.DictReader(f)}


def market_data(cur, insts):
    """종목별 (거래일, 종가) 목록과 원/달러 기준율(날짜 → 값)."""
    cur.execute("select symbol, trade_date, close_price from source.daily_prices order by symbol, trade_date")
    days = defaultdict(list)
    for s, d, c in cur.fetchall():
        if s in insts:
            days[s].append((d, c))
    cur.execute("select rate_date, rate from source.fx_rates where currency_pair = 'USDKRW' order by rate_date")
    fx = cur.fetchall()
    return dict(days), fx


def fx_on(fx, d):
    """그날, 없으면 직전 고시일의 값."""
    lo, hi, best = 0, len(fx) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if fx[mid][0] <= d:
            best, lo = fx[mid][1], mid + 1
        else:
            hi = mid - 1
    return best


def make_trades(n, first_id, rng, insts, days, fx):
    """매매 n건(주문, 체결 행). 날짜에 환율이 없는 미국 매매는 건너뛰고 다시 고릅니다."""
    syms = sorted(days)
    out, oid = [], first_id
    while len(out) < n:
        s = rng.choice(syms)
        d, close = rng.choice(days[s])
        market = insts[s]["market"]
        rate = None if market == "KRX" else fx_on(fx, d)
        if market != "KRX" and rate is None:
            continue
        tz, t = CLOSE[market]
        at = datetime.combine(d, t, tz).astimezone(timezone.utc)
        q = rng.randint(1, 20)
        out.append({"order_id": oid, "execution_id": oid + EXEC_OFFSET, "symbol": s, "qty": q, "price": close,
                    "fx_rate": rate, "at": at})
        oid += 1
    return out


def write(cur, trades, now):
    """주문, 체결을 넣고 종목마다 잔고를 한 번 더함(수량 합, 평균 단가는 가중 평균, 넷째 자리 반올림)."""
    cur.executemany(
        "insert into source.orders (order_id, account_id, symbol, side, order_type, order_qty, limit_price, filled_qty, "
        "status, ordered_at, updated_at) values (%s, %s, %s, 'BUY', 'MARKET', %s, null, %s, 'FILLED', %s, %s)",
        [(t["order_id"], ACCOUNT, t["symbol"], t["qty"], t["qty"], t["at"], now) for t in trades])
    cur.executemany(
        "insert into source.executions (execution_id, order_id, exec_qty, exec_price, fx_rate, executed_at, updated_at) "
        "values (%s, %s, %s, %s, %s, %s, %s)",
        [(t["execution_id"], t["order_id"], t["qty"], t["price"], t["fx_rate"], t["at"], now) for t in trades])
    agg = defaultdict(lambda: [0, Decimal(0)])
    for t in trades:
        agg[t["symbol"]][0] += t["qty"]
        agg[t["symbol"]][1] += t["qty"] * t["price"]
    cur.executemany(
        "insert into source.holdings (account_id, symbol, qty, avg_price, updated_at) values (%s, %s, %s, %s, %s) "
        "on conflict (account_id, symbol) do update set qty = holdings.qty + excluded.qty, "
        "avg_price = round((holdings.qty * holdings.avg_price + excluded.qty * excluded.avg_price) "
        "/ (holdings.qty + excluded.qty), 4), updated_at = excluded.updated_at",
        [(ACCOUNT, s, q, (v / q).quantize(Q4, ROUND_HALF_UP), now) for s, (q, v) in sorted(agg.items())])


def lsn(cur, insert=False):
    cur.execute(f"select ({'pg_current_wal_insert_lsn' if insert else 'pg_current_wal_lsn'}() - '0/0'::pg_lsn)::bigint")
    return int(cur.fetchone()[0])


def next_id(cur):
    cur.execute("select coalesce(max(order_id) + 1, %s) from source.orders where account_id = %s", (ORDER_BASE, ACCOUNT))
    return int(cur.fetchone()[0])


def counts(cur):
    out = {}
    for t, where in (("orders", "account_id = %s"), ("holdings", "account_id = %s"),
                     ("executions", "order_id in (select order_id from source.orders where account_id = %s)")):
        cur.execute(f"select count(*) from source.{t} where {where}", (ACCOUNT,))
        out[t] = int(cur.fetchone()[0])
    return out


def log_run(rec):
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    with RUNS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))


def run(kind, a):
    from common import connect, load_env
    insts = instruments()
    with connect(load_env()) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            days, fx = market_data(cur, insts)
            first = next_id(cur)
            lsn_from = lsn(cur, insert=True)
            conn.commit()
            rng = random.Random(f"{a.name}:{first}")
            t0 = time.time()
            started = datetime.now(timezone.utc)
            txns = 0
            if kind == "steady":
                total = a.rate * a.seconds
                trades = make_trades(total, first, rng, insts, days, fx)
                for i, tr in enumerate(trades):
                    due = t0 + i / a.rate
                    wait = due - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    write(cur, [tr], datetime.now(timezone.utc))
                    conn.commit()
                    txns += 1
            else:
                trades = make_trades(a.count, first, rng, insts, days, fx)
                for i in range(0, len(trades), a.batch):
                    write(cur, trades[i:i + a.batch], datetime.now(timezone.utc))
                    conn.commit()
                    txns += 1
            secs = time.time() - t0
            lsn_to = lsn(cur)
            conn.commit()
            log_run({"name": a.name, "kind": kind, "trades": len(trades), "transactions": txns,
                     "rate_asked": getattr(a, "rate", None), "batch": getattr(a, "batch", None),
                     "lsn_from": lsn_from, "lsn_to": lsn_to, "started": started.isoformat(timespec="milliseconds"),
                     "ended": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "write_seconds": round(secs, 2), "write_rate": round(len(trades) / secs, 1) if secs else None,
                     "first_order_id": first, "source_rows_after": counts(cur)})
            conn.commit()
    return 0


def clean(a):
    """A-LOAD 행을 모두 지움. 체결은 REPLICA IDENTITY FULL이라 지움 이벤트에 지우기 전 값이 모두 실립니다."""
    from common import connect, load_env
    with connect(load_env()) as conn, conn.cursor() as cur:
        lsn_from = lsn(cur, insert=True)
        conn.commit()
        t0, started, txns, gone = time.time(), datetime.now(timezone.utc), 0, defaultdict(int)
        cur.execute("select order_id from source.orders where account_id = %s order by order_id", (ACCOUNT,))
        ids = [r[0] for r in cur.fetchall()]
        for i in range(0, len(ids), a.batch):
            part = ids[i:i + a.batch]
            cur.execute("delete from source.executions where order_id = any(%s)", (part,))
            gone["executions"] += cur.rowcount
            cur.execute("delete from source.orders where order_id = any(%s)", (part,))
            gone["orders"] += cur.rowcount
            conn.commit()
            txns += 1
        cur.execute("delete from source.holdings where account_id = %s", (ACCOUNT,))
        gone["holdings"] += cur.rowcount
        conn.commit()
        txns += 1
        secs = time.time() - t0
        lsn_to = lsn(cur)
        conn.commit()
        log_run({"name": a.name, "kind": "clean", "trades": gone["executions"], "deleted": dict(gone), "transactions": txns,
                 "batch": a.batch, "lsn_from": lsn_from, "lsn_to": lsn_to, "started": started.isoformat(timespec="milliseconds"),
                 "ended": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "write_seconds": round(secs, 2),
                 "source_rows_after": counts(cur)})
        conn.commit()
    return 0


def selftest():
    from datetime import date
    insts = {"005930": {"market": "KRX"}, "SPY": {"market": "US"}}
    days = {"005930": [(date(2025, 3, 3), Decimal("55000")), (date(2025, 3, 4), Decimal("56000"))],
            "SPY": [(date(2025, 3, 3), Decimal("580.1")), (date(2025, 3, 7), Decimal("575.2"))]}
    fx = [(date(2025, 3, 3), Decimal("1450.1")), (date(2025, 3, 5), Decimal("1460.2"))]
    tr = make_trades(200, ORDER_BASE, random.Random(1), insts, days, fx)
    us = [t for t in tr if t["symbol"] == "SPY"]
    kr = [t for t in tr if t["symbol"] == "005930"]
    checks = [
        ("번호는 이어지고 체결은 주문 + 50,000,000", [t["order_id"] for t in tr] == list(range(ORDER_BASE, ORDER_BASE + 200))
         and all(t["execution_id"] == t["order_id"] + EXEC_OFFSET for t in tr)),
        ("국내는 환율 없음, 미국은 그날 또는 직전 환율", all(t["fx_rate"] is None for t in kr) and all(
            t["fx_rate"] == (Decimal("1450.1") if t["at"].date() <= date(2025, 3, 4) else Decimal("1460.2")) for t in us)),
        ("시각은 장 마감(서울 06:30 UTC, 뉴욕 21:00 UTC)", all(t["at"].hour == 6 and t["at"].minute == 30 for t in kr)
         and all(t["at"].hour == 21 for t in us)),
        ("주말 거래일 없음", all(t["at"].astimezone(CLOSE["KRX" if t["fx_rate"] is None else "US"][0]).weekday() < 5 for t in tr)),
        ("같은 이름이면 같은 매매", make_trades(50, 1, random.Random("a:1"), insts, days, fx)
         == make_trades(50, 1, random.Random("a:1"), insts, days, fx)),
        ("환율이 없는 날의 미국 매매는 다시 고름", not any(t["at"].date() < date(2025, 3, 3) for t in us)),
    ]
    for name, ok in checks:
        print(f"  {'통과' if ok else '실패'}  {name}")
    good = all(ok for _, ok in checks)
    print("자체 시험:", "통과" if good else "실패")
    return 0 if good else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("steady")
    p.add_argument("--name", required=True)
    p.add_argument("--rate", type=int, default=20)
    p.add_argument("--seconds", type=int, default=180)
    p = sub.add_parser("burst")
    p.add_argument("--name", required=True)
    p.add_argument("--count", type=int, default=10000)
    p.add_argument("--batch", type=int, default=500)
    p = sub.add_parser("clean")
    p.add_argument("--name", default="clean")
    p.add_argument("--batch", type=int, default=5000)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.cmd in ("steady", "burst"):
        sys.exit(run(a.cmd, a))
    if a.cmd == "clean":
        sys.exit(clean(a))
    if a.cmd == "status":
        from common import connect, load_env
        with connect(load_env()) as conn, conn.cursor() as cur:
            print(counts(cur))
        sys.exit(0)
    sys.exit(__doc__)
