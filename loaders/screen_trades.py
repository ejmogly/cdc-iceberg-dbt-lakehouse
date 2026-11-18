#!/usr/bin/env python3
"""화면에 넣은 매매를 원천 PostgreSQL의 주문, 체결, 잔고로 옮기고, 레이크하우스를 거쳐 Gold에 같은 값으로 닿았는지 봅니다
(docs/ai/F-screen.md 7절 「내 매매도 CDC로」).

사용(저장소 루트에서):
  .venv/bin/python loaders/screen_trades.py --dry-run   원천 DB와 견줘 넣을 것, 고칠 것, 지울 것만 셈
  .venv/bin/python loaders/screen_trades.py             원천에 씀(원천 DB가 떠 있어야 함)
  .venv/bin/python loaders/screen_trades.py --verify    Gold(dbt fct_execution)를 읽어 화면 매매, 화면 손익과 대조하고
                                                        data/generated/f_screen/trades_lake.json에 결과를 씀(Spark 필요)
  .venv/bin/python loaders/screen_trades.py --selftest  DB 없이 바꿀 것 계산 규칙을 시험

옮기는 규칙
- 계좌는 A-SCREEN 하나입니다(원천 시나리오 계좌 A-0001과 나눔).
- 매매 한 줄 = 주문 한 건(시장가, 전량 체결) + 체결 한 건. 체결가와 환율은 화면 계산 서버가 손익을 셀 때 쓴 값
  (비운 칸은 그날 종가와 매매기준율, 휴장일은 직전 거래일)을 그대로 씁니다. 그래서 원천의 체결과 화면 손익이 같은 값에서 나옵니다.
- 계산에서 빠지는 줄(보유보다 많이 팖, 기준일 뒤, 수량 0 이하, 데이터 기간 밖, 소수 수량)은 넣지 않습니다.
- 체결 시각은 그 시장의 장 마감(서울 15:30, 뉴욕 16:00)이고, 날짜는 계산에 쓴 날(휴장일 매매는 직전 거래일)입니다.
  Gold의 fct_execution이 시장 현지 날짜로 거래일을 셉니다.
- 잔고는 계산 끝의 남은 수량과 평균 단가(소수 넷째 자리, 반올림 절반 올림). 남은 수량이 0이면 지웁니다.
- 번호는 주문 900000000 + 종목 순번 x 1000 + 줄 번호, 체결은 같은 번호 + 100000000입니다. 저장 파일의 줄 순서가 같으면
  번호가 같아 다시 돌려도 값이 바뀐 줄만 고칩니다. 값이 같은 줄은 고치지 않아 CDC 이벤트가 생기지 않습니다(loaders/common.py).
  저장 파일에서 사라진 줄은 지웁니다(체결 → 주문 순서).
- 저장 파일은 읽기만 합니다. 매매 금액과 가격은 출력하지 않습니다(행 수와 번호만).
"""
import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loaders"))

ACCOUNT = "A-SCREEN"
ORDER_BASE, EXEC_OFFSET = 900_000_000, 100_000_000
TRADES = ROOT / "data" / "generated" / "f_screen" / "screen_trades.json"
OUT = ROOT / "data" / "generated" / "f_screen" / "trades_lake.json"
CLOSE = {"KRX": (ZoneInfo("Asia/Seoul"), time(15, 30)), "US": (ZoneInfo("America/New_York"), time(16, 0))}
Q4 = Decimal("0.0001")


def instruments():
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f)]


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def plan_rows(saved, pnl_fn, insts):
    """저장 파일과 손익 계산 함수로 원천에 있어야 할 (주문, 체결, 잔고) 행을 만듭니다.
    pnl_fn(symbol, asof, trades) → 계산 서버의 /api/pnl 응답. 금액은 Decimal, 시각은 UTC aware datetime."""
    order_of = {r["symbol"]: i for i, r in enumerate(insts)}
    market = {r["symbol"]: r["market"] for r in insts}
    currency = {r["symbol"]: r["currency"] for r in insts}
    orders, execs, holds, results = {}, {}, {}, {}
    for symbol, v in sorted(saved.get("symbols", {}).items()):
        trades = [t for t in v.get("trades", []) if t.get("date") and t.get("qty")]
        if symbol not in order_of or not trades:
            continue
        res = pnl_fn(symbol, v["asof"], trades)
        results[symbol] = res
        tz, close = CLOSE[market[symbol]]
        for s in res["steps"]:
            if s["excluded"]:
                continue
            k = s["trade_index"]
            oid = ORDER_BASE + order_of[symbol] * 1000 + k
            at = datetime.combine(date.fromisoformat(s["date"]), close, tz).astimezone(timezone.utc)
            qty = int(Decimal(str(s["qty"])))
            orders[oid] = {"order_id": oid, "account_id": ACCOUNT, "symbol": symbol, "side": s["side"],
                           "order_type": "MARKET", "order_qty": qty, "limit_price": None, "filled_qty": qty,
                           "status": "FILLED", "ordered_at": at}
            execs[oid + EXEC_OFFSET] = {"execution_id": oid + EXEC_OFFSET, "order_id": oid, "exec_qty": qty,
                                        "exec_price": Decimal(str(s["price"])).quantize(Q4),
                                        "fx_rate": None if currency[symbol] == "KRW" else Decimal(str(s["fx"])).quantize(Q4),
                                        "executed_at": at}
        summ = res.get("summary")
        if summ and Decimal(str(summ["qty_left"])) > 0:
            holds[symbol] = {"account_id": ACCOUNT, "symbol": symbol, "qty": int(Decimal(str(summ["qty_left"]))),
                             "avg_price": Decimal(str(summ["avg_cost_local"])).quantize(Q4, ROUND_HALF_UP)}
    return orders, execs, holds, results


def diff(want, have, keys):
    """(넣을 것, 고칠 것, 지울 것). keys는 값을 견줄 칸(시각 칸 updated_at은 견주지 않음)."""
    add = [k for k in want if k not in have]
    change = [k for k in want if k in have and any(want[k][c] != have[k][c] for c in keys)]
    drop = [k for k in have if k not in want]
    return add, change, drop


# 화면 손익과 Gold 손익 마트(pnl_position)를 견줄 칸: E01 정답 칸(eval/e01_pnl/check_sql.py EXACT)과 연 환산(0.05%p 안)
PNL_EXACT = ("qty_left", "avg_cost_local", "avg_buy_fx", "realized_local", "unrealized_local", "total_local", "realized_krw",
             "unrealized_krw", "total_krw", "invested_krw", "simple_return_pct", "price_effect_krw", "fx_effect_krw",
             "cross_effect_krw", "bench_total_krw", "max_drawdown_krw")
PNL_NEAR = ("irr_pct", "bench_irr_pct")

ORDER_KEYS = ("account_id", "symbol", "side", "order_type", "order_qty", "limit_price", "filled_qty", "status", "ordered_at")
EXEC_KEYS = ("order_id", "exec_qty", "exec_price", "fx_rate", "executed_at")
HOLD_KEYS = ("qty", "avg_price")


def read_have(cur):
    cols = ", ".join(("order_id",) + ORDER_KEYS)
    cur.execute(f"select {cols} from source.orders where account_id = %s", (ACCOUNT,))
    orders = {r[0]: dict(zip(("order_id",) + ORDER_KEYS, r)) for r in cur.fetchall()}
    cols = ", ".join(("execution_id",) + EXEC_KEYS)
    cur.execute(f"select {cols} from source.executions e where e.order_id in "
                f"(select order_id from source.orders where account_id = %s)", (ACCOUNT,))
    execs = {r[0]: dict(zip(("execution_id",) + EXEC_KEYS, r)) for r in cur.fetchall()}
    cur.execute("select symbol, qty, avg_price from source.holdings where account_id = %s", (ACCOUNT,))
    holds = {r[0]: {"account_id": ACCOUNT, "symbol": r[0], "qty": r[1], "avg_price": r[2]} for r in cur.fetchall()}
    return orders, execs, holds


def pnl_from_server():
    """계산 서버(ai/screen_server.App)를 화면 데이터 파일로 띄우지 않고 바로 부릅니다(서버 손익과 같은 규칙)."""
    from ai.screen_server import App
    app = App(ROOT / "data" / "generated" / "screen.duckdb")
    if not app.status["ok"]:
        sys.exit("화면 데이터 파일의 검사가 실패했습니다. make screen-data를 먼저 돌리세요.")
    return lambda symbol, asof, trades: app.api_pnl({"symbol": symbol, "asof": asof, "trades": trades})


def sync(dry_run, trades_path=TRADES):
    from common import connect, load_env
    saved = json.loads(trades_path.read_text(encoding="utf-8"))
    orders, execs, holds, _ = plan_rows(saved, pnl_from_server(), instruments())
    now = datetime.now(timezone.utc)
    with connect(load_env()) as conn, conn.cursor() as cur:
        h_orders, h_execs, h_holds = read_have(cur)
        plan = {"주문": diff(orders, h_orders, ORDER_KEYS), "체결": diff(execs, h_execs, EXEC_KEYS),
                "잔고": diff(holds, h_holds, HOLD_KEYS)}
        want = {"주문": orders, "체결": execs, "잔고": holds}
        for name, (a, c, d) in plan.items():
            print(f"  {name}: 넣을 것 {len(a)}, 고칠 것 {len(c)}, 지울 것 {len(d)}, 그대로 {len(want[name]) - len(a) - len(c)}")
        if dry_run:
            conn.rollback()
            return 0
        # 지우기: 체결 → 주문(외래 키), 잔고
        (_, _, d_exec), (_, _, d_order), (_, _, d_hold) = plan["체결"], plan["주문"], plan["잔고"]
        if d_exec:
            cur.execute("delete from source.executions where execution_id = any(%s)", (d_exec,))
        if d_order:
            cur.execute("delete from source.orders where order_id = any(%s)", (d_order,))
        if d_hold:
            cur.execute("delete from source.holdings where account_id = %s and symbol = any(%s)", (ACCOUNT, d_hold))
        a, c, _ = plan["주문"]
        for k in a + c:
            o = orders[k]
            cur.execute(
                "insert into source.orders (order_id, account_id, symbol, side, order_type, order_qty, limit_price, "
                "filled_qty, status, ordered_at, updated_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (order_id) do update set account_id = excluded.account_id, symbol = excluded.symbol, "
                "side = excluded.side, order_type = excluded.order_type, order_qty = excluded.order_qty, "
                "limit_price = excluded.limit_price, filled_qty = excluded.filled_qty, status = excluded.status, "
                "ordered_at = excluded.ordered_at, updated_at = excluded.updated_at",
                (o["order_id"], o["account_id"], o["symbol"], o["side"], o["order_type"], o["order_qty"], o["limit_price"],
                 o["filled_qty"], o["status"], o["ordered_at"], now))
        a, c, _ = plan["체결"]
        for k in a + c:
            e = execs[k]
            cur.execute(
                "insert into source.executions (execution_id, order_id, exec_qty, exec_price, fx_rate, executed_at, updated_at) "
                "values (%s, %s, %s, %s, %s, %s, %s) on conflict (execution_id) do update set order_id = excluded.order_id, "
                "exec_qty = excluded.exec_qty, exec_price = excluded.exec_price, fx_rate = excluded.fx_rate, "
                "executed_at = excluded.executed_at, updated_at = excluded.updated_at",
                (e["execution_id"], e["order_id"], e["exec_qty"], e["exec_price"], e["fx_rate"], e["executed_at"], now))
        a, c, _ = plan["잔고"]
        for k in a + c:
            h = holds[k]
            cur.execute(
                "insert into source.holdings (account_id, symbol, qty, avg_price, updated_at) values (%s, %s, %s, %s, %s) "
                "on conflict (account_id, symbol) do update set qty = excluded.qty, avg_price = excluded.avg_price, "
                "updated_at = excluded.updated_at", (h["account_id"], h["symbol"], h["qty"], h["avg_price"], now))
        conn.commit()
    print(f"원천에 씀(계좌 {ACCOUNT}). 매매 파일 {trades_path.name}, sha256 {file_sha(trades_path)[:12]}")
    return 0


def verify(trades_path=TRADES):
    """Gold fct_execution(계좌 A-SCREEN)을 화면 매매에서 만든 체결과 견주고, Gold 체결만으로 다시 센 손익을 화면 손익과 견줍니다."""
    from ai.screen_data import _spark_rows
    saved = json.loads(trades_path.read_text(encoding="utf-8"))
    pnl = pnl_from_server()
    orders, execs, holds, results = plan_rows(saved, pnl, instruments())
    rows = _spark_rows("select execution_id, order_id, symbol, side, exec_qty, exec_price, fx_rate, trade_date "
                       f"from lake.silver.fct_execution where account_id = '{ACCOUNT}'")
    gold = {int(r[0]): [("" if x == "NULL" else x) for x in r] for r in rows}
    bad = []
    for k, e in execs.items():
        g = gold.get(k)
        o = orders[e["order_id"]]
        want = (str(e["order_id"]), o["symbol"], o["side"], str(e["exec_qty"]), str(e["exec_price"]),
                "" if e["fx_rate"] is None else str(e["fx_rate"]),
                e["executed_at"].astimezone(CLOSE["KRX" if e["fx_rate"] is None else "US"][0]).date().isoformat())
        if g is None:
            bad.append(f"체결 {k} Gold에 없음")
        elif tuple(g[1:]) != want:
            bad.append(f"체결 {k} 값 다름")
    bad += [f"체결 {k} 원천 계획에 없음" for k in gold if k not in execs]
    # Gold 체결만으로(체결가와 환율을 넣어) 다시 센 손익이 화면 손익과 같은지
    same, diff_syms = 0, []
    for symbol, res in results.items():
        g_trades = [{"date": r[7], "side": r[3], "qty": r[4], "price": r[5], **({} if r[6] == "" else {"fx_rate": r[6]})}
                    for r in sorted((g for g in gold.values() if g[2] == symbol), key=lambda g: (g[7], int(g[0])))]
        if not g_trades:
            continue
        again = pnl(symbol, saved["symbols"][symbol]["asof"], g_trades)
        keys = ("total_krw", "realized_krw", "unrealized_krw", "qty_left", "price_effect_krw", "fx_effect_krw")
        if all(again["summary"][x] == res["summary"][x] for x in keys):
            same += 1
        else:
            diff_syms.append(symbol)
    # Gold 손익 마트(dbt/models/pnl, 손익 SQL의 Spark 판)가 화면 손익과 칸마다 같은지. 기준일이 같은 종목만 견줍니다
    gold_asof = _spark_rows("select max(cal_date) from lake.gold.pnl_calendar")[0][0]
    cols = ("position_id",) + PNL_EXACT + PNL_NEAR
    gp = {r[0]: dict(zip(cols, r)) for r in _spark_rows(
        f"select {', '.join(cols)} from lake.gold.pnl_position where position_id like '{ACCOUNT}:%'")}
    g_same, g_diff, g_skip = 0, [], []
    for symbol, res in results.items():
        if saved["symbols"][symbol]["asof"] != gold_asof:
            g_skip.append(symbol)
            continue
        g = gp.get(f"{ACCOUNT}:{symbol}")
        s_ = res["summary"]
        dec = lambda v: None if v in (None, "", "NULL") else Decimal(str(v))
        bad_cols = [] if g else ["행 없음"]
        if g:
            bad_cols = [c for c in PNL_EXACT if dec(g[c]) != dec(s_[c])]
            bad_cols += [c for c in PNL_NEAR if (dec(g[c]) is None) != (dec(s_[c]) is None)
                         or (dec(g[c]) is not None and abs(dec(g[c]) - dec(s_[c])) > Decimal("0.05"))]
        if bad_cols:
            g_diff.append(f"{symbol}: {', '.join(bad_cols)}")
        else:
            g_same += 1
    out = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "account": ACCOUNT,
           "trades_file": trades_path.name, "trades_sha256": file_sha(trades_path), "symbols": len(results), "executions_want": len(execs),
           "executions_gold": len(gold), "executions_ok": not bad, "problems": bad[:10],
           "pnl_same": same, "pnl_checked": same + len(diff_syms), "pnl_diff_symbols": diff_syms,
           "gold_pnl_asof": gold_asof, "gold_pnl_same": g_same, "gold_pnl_checked": g_same + len(g_diff),
           "gold_pnl_diff": g_diff, "gold_pnl_skipped_asof": g_skip,
           "path": "원천 주문, 체결, 잔고 → Debezium, Kafka → Flink Bronze → Spark MERGE Silver → dbt fct_execution → dbt pnl_position"}
    if trades_path == TRADES:  # 시험용 매매 파일의 결과는 화면이 보이는 기록에 쓰지 않음
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Gold 체결 {len(gold)}건 / 화면 매매에서 만든 체결 {len(execs)}건, {'같음' if not bad else f'{len(bad)}건 다름'}; "
          f"Gold 체결로 다시 센 손익 {same}/{same + len(diff_syms)}종목 같음; "
          f"Gold 손익 마트(pnl_position, 기준일 {gold_asof}) {g_same}/{g_same + len(g_diff)}종목 칸마다 같음"
          + (f"(기준일이 달라 건너뜀 {len(g_skip)})" if g_skip else "") + (f" 다름: {g_diff}" if g_diff else "")
          + (f" → {OUT.relative_to(ROOT)}" if trades_path == TRADES else "(시험용 매매 파일이라 기록하지 않음)"))
    return 0 if not bad and not diff_syms and not g_diff else 1


def selftest():
    """DB 없이: 가짜 손익 함수로 계획을 만들고, 줄 하나를 고치거나 빼면 diff가 그만큼만 잡는지 봅니다."""
    insts = [{"symbol": "005930", "market": "KRX", "currency": "KRW"}, {"symbol": "QQQ", "market": "US", "currency": "USD"}]

    def fake(symbol, asof, trades):
        steps = [{"trade_index": i, "date": t["date"], "side": t["side"], "qty": t["qty"], "price": t.get("price", "100"),
                  "fx": t.get("fx_rate", "1400") if symbol == "QQQ" else 1, "excluded": t.get("over", False)}
                 for i, t in enumerate(trades)]
        left = sum((1 if s["side"] == "BUY" else -1) * int(s["qty"]) for s in steps if not s["excluded"])
        return {"steps": steps, "summary": {"qty_left": left, "avg_cost_local": "100.12345"}}

    saved = {"symbols": {"005930": {"asof": "2026-11-12", "trades": [{"date": "2025-03-04", "side": "BUY", "qty": "10"}]},
                         "QQQ": {"asof": "2026-11-12", "trades": [
                             {"date": "2025-03-03", "side": "BUY", "qty": "10"}, {"date": "2025-08-01", "side": "BUY", "qty": "5"},
                             {"date": "2026-02-02", "side": "SELL", "qty": "8"}, {"date": "2026-03-02", "side": "SELL", "qty": "99", "over": True}]}}}
    o1, e1, h1, _ = plan_rows(saved, fake, insts)
    checks = [("빠지는 줄은 넣지 않음", len(e1) == 4),
              ("국내 체결 환율은 비움", e1[EXEC_OFFSET + ORDER_BASE]["fx_rate"] is None),
              ("뉴욕 장 마감 시각(UTC 21시)", e1[EXEC_OFFSET + ORDER_BASE + 1000]["executed_at"].hour == 21),
              ("잔고 수량과 평균 단가 넷째 자리", h1["QQQ"]["qty"] == 7 and str(h1["QQQ"]["avg_price"]) == "100.1235"),
              ("같은 계획은 바꿀 것 0", diff(e1, e1, EXEC_KEYS) == ([], [], []))]
    saved["symbols"]["QQQ"]["trades"][1]["qty"] = "6"
    o2, e2, h2, _ = plan_rows(saved, fake, insts)
    a, c, d = diff(e2, e1, EXEC_KEYS)
    checks.append(("수량 하나 고치면 체결 하나만 고침", (len(a), len(c), len(d)) == (0, 1, 0)))
    del saved["symbols"]["QQQ"]["trades"][1]  # 가운데 줄을 빼면 뒤 줄이 그 번호로 당겨짐(고침 하나), 마지막 번호는 지움
    o3, e3, h3, _ = plan_rows(saved, fake, insts)
    a, c, d = diff(e3, e2, EXEC_KEYS)
    checks.append(("가운데 줄을 빼면 고침 하나, 지움 하나", (len(a), len(c), len(d)) == (0, 1, 1)))
    del saved["symbols"]["005930"]
    o4, e4, h4, _ = plan_rows(saved, fake, insts)
    a, c, d = diff(h4, h3, HOLD_KEYS)
    checks.append(("종목을 비우면 잔고를 지움", d == ["005930"]))
    for name, ok in checks:
        print(f"  {'통과' if ok else '실패'}  {name}")
    good = all(ok for _, ok in checks)
    print("자체 시험:", "통과" if good else "실패")
    return 0 if good else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--trades", type=Path, default=TRADES, help="매매 파일(기본은 화면 저장 파일). 시험용 파일은 data/generated/ 아래")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.verify:
        sys.exit(verify(a.trades))
    sys.exit(sync(a.dry_run, a.trades))
