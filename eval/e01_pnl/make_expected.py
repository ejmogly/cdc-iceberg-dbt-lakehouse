#!/usr/bin/env python3
"""E01 손익 평가의 정답을 만든다. 사용: python3 make_expected.py [--explain G08]

검사 대상과 다른 방식으로 계산한다. 분수(Fraction)로 정확히 계산하고, 평균 단가 대신
보유 원가를 들고 다니며, 반올림은 맨 끝에서 한 번만 한다. 규칙은 docs/ai/experiments/E01-pnl.md.
"""
import json
import sys
from fractions import Fraction as F
from pathlib import Path

HERE = Path(__file__).parent
FIX = json.loads((HERE / "fixture.json").read_text(encoding="utf-8"))
CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
CAL = FIX["calendar"]


def frac(v):
    return F(str(v))


def snap(date):
    """달력에서 date 이하 마지막 날."""
    return max(d for d in CAL if d <= date)


def carry(table, date, key=None):
    d = max(k for k in table if k <= date)
    return frac(table[d][key] if key else table[d])


def close(sym, date):
    return carry(FIX["symbols"][sym]["rows"], date, "close")


def rate(date):
    return carry(FIX["usdkrw"], date)


def rnd(x, places=0):
    """0.5는 0에서 먼 쪽으로 반올림."""
    m = 10 ** places
    v = abs(x) * m
    n = v.numerator // v.denominator
    if v - n >= F(1, 2):
        n += 1
    n = F(n, m)
    n = -n if x < 0 else n
    return int(n) if places == 0 else float(n)


def days(a, b):
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def irr(flows):
    """연 환산 내부수익률(%). 첫 매매일부터 30일 미만이거나 부호 변화가 없으면 None."""
    if len(flows) < 2 or days(flows[0][0], flows[-1][0]) < 30:
        return None
    t0 = flows[0][0]
    fl = [(days(t0, d) / 365, float(v)) for d, v in flows]

    def npv(r):
        return sum(v / (1 + r) ** t for t, v in fl)
    lo, hi = -0.99, 1000.0
    a, b = npv(lo), npv(hi)
    if a * b > 0:
        return None
    for _ in range(200):
        m = (lo + hi) / 2
        v = npv(m)
        if a * v <= 0:
            hi = m
        else:
            lo, a = m, v
    return round((lo + hi) / 2 * 100, 2)


def run(case, log=None):
    sym, asof = case["symbol"], case["asof"]
    usd = FIX["symbols"][sym]["currency"] == "USD"
    end = snap(asof)
    warnings, ok = [], []
    order = sorted(range(len(case["trades"])), key=lambda k: (snap(case["trades"][k]["d"]), k))
    for k in order:
        t = case["trades"][k]
        if t["q"] <= 0:
            warnings.append({"code": "BAD_QTY", "trade": k})
            continue
        if snap(t["d"]) > end:
            warnings.append({"code": "AFTER_ASOF", "trade": k})
            continue
        ok.append((k, t))

    qty = 0
    cost_l = cost_k = real_l = real_k = F(0)
    eff_p = eff_f = eff_c = F(0)
    invested = invested_l = F(0)
    b_q = b_in = b_cash = F(0)
    flows, bflows, sells, ledger = [], [], [], []
    peak, dd = F(0), F(0)
    start = snap(ok[0][1]["d"]) if ok else end
    p = 0

    def bpx(d):
        return close("BENCH", d) * rate(d)

    def add_effects(q, px, fx):
        af = cost_k / cost_l
        avg = cost_l / qty
        return q * (px - avg) * af, q * avg * (fx - af), q * (px - avg) * (fx - af)

    for d in CAL:
        if d < start or d > end:
            continue
        while p < len(ok) and snap(ok[p][1]["d"]) == d:
            k, t = ok[p]
            p += 1
            rows = FIX["symbols"][sym]["rows"]
            if t["d"] not in rows:
                warnings.append({"code": "NON_TRADING_DAY", "trade": k})
            q = t["q"]
            px = frac(t["p"]) if "p" in t else close(sym, d)
            if "p" in t and t["d"] in rows:
                if not frac(rows[t["d"]]["low"]) <= px <= frac(rows[t["d"]]["high"]):
                    warnings.append({"code": "PRICE_OUT_OF_RANGE", "trade": k})
            fx = (frac(t["fx"]) if "fx" in t else rate(d)) if usd else F(1)
            if t["side"] == "BUY":
                amt = q * px * fx
                qty += q
                cost_l += q * px
                cost_k += amt
                invested += amt
                invested_l += q * px
                flows.append((d, -amt))
                bflows.append((d, -amt))
                b_q += amt / bpx(d)
                b_in += amt
            else:
                if q > qty:
                    warnings.append({"code": "OVERSOLD", "trade": k})
                    continue
                e = add_effects(q, px, fx)
                eff_p += e[0]
                eff_f += e[1]
                eff_c += e[2]
                out_l = cost_l * F(q, qty)
                out_k = cost_k * F(q, qty)
                g_k = q * px * fx - out_k
                sells.append({"d": d, "q": q, "price": rnd(px, 4),
                              "avg_cost": rnd(cost_l / qty, 4), "pnl_krw": rnd(g_k)})
                real_l += q * px - out_l
                real_k += g_k
                flows.append((d, q * px * fx))
                part = b_q * F(q, qty)
                b_q -= part
                b_cash += part * bpx(d)
                bflows.append((d, part * bpx(d)))
                qty -= q
                cost_l -= out_l
                cost_k -= out_k
            if log is not None:
                log.append((d, t["side"], q, px, fx, qty, cost_l, cost_k, real_k))
        fx_d = rate(d) if usd else F(1)
        v = real_k + qty * close(sym, d) * fx_d - cost_k
        peak = max(peak, v)
        dd = min(dd, v - peak)

    px_e = close(sym, end)
    fx_e = rate(end) if usd else F(1)
    if qty:
        e = add_effects(qty, px_e, fx_e)
        eff_p += e[0]
        eff_f += e[1]
        eff_c += e[2]
    un_l = qty * px_e - cost_l
    un_k = qty * px_e * fx_e - cost_k
    total_k = real_k + un_k
    total_l = real_l + un_l
    assert eff_p + eff_f + eff_c == total_k, case["id"]
    b_total = b_cash + b_q * bpx(end) - b_in
    flows.append((end, qty * px_e * fx_e))
    bflows.append((end, b_q * bpx(end)))
    lp = 2 if usd else 0
    return {
        "qty_left": qty,
        "avg_cost_local": rnd(cost_l / qty, 4) if qty else None,
        "avg_buy_fx": rnd(cost_k / cost_l, 4) if qty and usd else None,
        "realized_local": rnd(real_l, lp),
        "unrealized_local": rnd(un_l, lp),
        "total_local": rnd(total_l, lp),
        "realized_krw": rnd(real_k),
        "unrealized_krw": rnd(un_k),
        "total_krw": rnd(total_k),
        "invested_krw": rnd(invested),
        "simple_return_pct": rnd(total_k / invested * 100, 2) if invested else None,
        "price_effect_krw": rnd(eff_p),
        "fx_effect_krw": rnd(eff_f),
        "cross_effect_krw": rnd(eff_c),
        "bench_total_krw": rnd(b_total),
        "max_drawdown_krw": rnd(dd),
        "irr_pct": irr(flows) if invested else None,
        "bench_irr_pct": irr(bflows) if invested else None,
        "naive_krw": rnd(total_l * fx_e),
        "sells": sells,
        "warnings": warnings,
    }


def explain(case_id):
    case = next(c for c in CASES if c["id"] == case_id)
    log = []
    r = run(case, log)
    print(f"{case_id} {case['title']} ({case['symbol']}, 기준일 {case['asof']})\n")
    print("| 날짜 | 구분 | 수량 | 가격 | 환율 | 뒤 수량 | 보유 원가(현지) | 보유 원가(원) | 누적 실현(원) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for d, side, q, px, fx, qty, cl, ck, rk in log:
        print(f"| {d} | {'매수' if side == 'BUY' else '매도'} | {q} | {float(px):,.2f} | {float(fx):,.2f} | {qty} | "
              f"{float(cl):,.4f} | {float(ck):,.2f} | {float(rk):,.2f} |")
    print()
    print(json.dumps({k: v for k, v in r.items() if k != "sells"}, ensure_ascii=False))


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--explain":
        explain(sys.argv[2])
        return
    out = {c["id"]: run(c) for c in CASES}
    with open(HERE / "expected.json", "w", encoding="utf-8") as f:
        f.write("{\n" + ",\n".join(f' "{k}": {json.dumps(v, ensure_ascii=False)}' for k, v in out.items()) + "\n}\n")
    print("| 사례 | 원화 손익 | 실현 | 평가 | 가격 | 환율 | 교차 | 낙폭 | 연 환산 % | 경고 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for k, r in out.items():
        print(f"| {k} | {r['total_krw']:,} | {r['realized_krw']:,} | {r['unrealized_krw']:,} | {r['price_effect_krw']:,} | "
              f"{r['fx_effect_krw']:,} | {r['cross_effect_krw']:,} | {r['max_drawdown_krw']:,} | {r['irr_pct']} | "
              f"{','.join(w['code'] for w in r['warnings'])} |")
    us = [c["id"] for c in CASES if c["symbol"] == "US1"]
    errs = [(k, abs(out[k]["naive_krw"] - out[k]["total_krw"]), abs(out[k]["total_krw"])) for k in us]
    m = sum(e for _, e, _ in errs) / len(errs)
    w = max(errs, key=lambda x: x[1])
    print(f"\n기준선(달러 손익 x 기준일 환율)의 미국 사례 {len(us)}개 평균 오차 {m:,.0f}원, 최대 {w[1]:,}원({w[0]})")


if __name__ == "__main__":
    main()
