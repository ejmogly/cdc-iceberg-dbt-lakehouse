#!/usr/bin/env python3
"""F 화면 평가: 실제 데이터 사례 12개와 경고 사례 8개의 정답을 만듭니다(docs/ai/F-screen.md 4절).

사용: .venv/bin/python eval/f_screen/make_expected_real.py

- 계산은 E01 독립 계산기(eval/e01_pnl/make_expected.py의 run)를 그대로 씁니다. 분수로 정확히 계산하고,
  손익 SQL과 화면 코드를 쓰지 않습니다. 데이터만 고정 시세 대신 `data/raw/` 원본(real_world.py)으로 바꿉니다.
- 사례 정의(cases_real.json)에는 가격을 적지 않습니다. 체결가는 규칙(p_rule)으로, 환율은 배수(fx_factor)로 적고
  여기서 원본 값을 읽어 정합니다.
- 손익 SQL에 없는 화면 경고 셋은 여기서 정의대로 붙입니다.
  OUT_OF_DATA_RANGE: 그 종목 시세의 첫날보다 앞이거나 마지막 날보다 뒤인 매매. 계산에서 뺌. 다른 경고는 붙이지 않음
  FRACTIONAL_QTY: 수량이 정수가 아닌 매매. 계산에서 뺌(E01 R16). 다른 경고는 붙이지 않음
  FX_CARRIED: 계산에 쓰인 미국 종목 매매 줄(초과 매도, 기준일 뒤, 수량 0 이하로 빠진 줄은 뺌)에 환율을 넣지 않았고,
    그 줄을 계산하는 날(달력에 맞춘 날)의 환율이 없어 직전 값을 씀. 기준일 평가에 쓴 직전 환율은 경고하지 않음
- 실제 데이터 사례(R)마다 보유 기간(첫 매매일 ~ 기준일)의 큰 움직임 날도 냅니다(E02 3절 정의: 단순 수익률,
  그날을 뺀 앞 60거래일 수익률의 표본 표준편차의 2배 초과, 수정한 가격). 앞 수익률 60개가 없는 날은 판정하지 않습니다.
- 정답은 data/generated/f_screen/expected_real.json(git 제외)에 둡니다. 출력에는 가격을 내지 않습니다.
"""
import importlib.util
import json
import sys
from fractions import Fraction as F
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import real_world  # noqa: E402

OUT = ROOT / "data" / "generated" / "f_screen" / "expected_real.json"


def calculator(world):
    spec = importlib.util.spec_from_file_location("e01_expected", ROOT / "eval" / "e01_pnl" / "make_expected.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    setattr(m, "FIX", world)
    setattr(m, "CAL", world["calendar"])
    return m


def price(m, sym, d, rule):
    row = m.FIX["symbols"][sym]["rows"][d]
    places = 2 if m.FIX["symbols"][sym]["currency"] == "USD" else 0
    low, high = F(row["low"]), F(row["high"])
    if rule == "mid":
        v = (low + high) / 2
    elif rule == "high_x1.2":
        v = high * F(6, 5)
    else:
        raise ValueError(rule)
    return str(m.rnd(v, places))


def solve(m, case):
    sym = case["symbol"]
    rows = m.FIX["symbols"][sym]["rows"]
    usd = m.FIX["symbols"][sym]["currency"] == "USD"
    first, last = min(rows), max(rows)
    pre, kept = [], []
    for k, t in enumerate(case["trades"]):
        if t["d"] < first or t["d"] > last:
            pre.append({"code": "OUT_OF_DATA_RANGE", "trade": k})
            continue
        if float(t["q"]) != int(t["q"]):
            pre.append({"code": "FRACTIONAL_QTY", "trade": k})
            continue
        u = {"d": t["d"], "side": t["side"], "q": int(t["q"])}
        if "p_rule" in t:
            u["p"] = price(m, sym, t["d"], t["p_rule"])
        if "fx_factor" in t:
            u["fx"] = str(m.rnd(m.rate(m.snap(t["d"])) * F(t["fx_factor"]), 2))
        kept.append((k, u))
    r = m.run({"id": case["id"], "symbol": sym, "asof": case["asof"], "trades": [u for _, u in kept]})
    idx = [k for k, _ in kept]
    dropped = {idx[w["trade"]] for w in r["warnings"] if w["code"] in ("OVERSOLD", "AFTER_ASOF", "BAD_QTY")}
    for k, u in kept:
        if usd and "fx" not in u and k not in dropped and m.snap(u["d"]) not in m.FIX["usdkrw"]:
            pre.append({"code": "FX_CARRIED", "trade": k})
    r["warnings"] = sorted(pre + [{"code": w["code"], "trade": idx[w["trade"]]} for w in r["warnings"]],
                           key=lambda w: (w["trade"], w["code"]))
    if case["group"] == "실제" and kept:
        r["big_moves"] = big_moves(sym, min(u["d"] for _, u in kept), case["asof"])
    r["inputs"] = {str(k): u for k, u in kept}
    return r


def adjusted_closes(sym):
    """수익률용 종가: 미국은 수정한 판(adjtrue), 국내는 그대로(F-screen.md 6절 2번)."""
    import json as _j
    from datetime import datetime, timezone
    raw = real_world.RAW / "daily_prices"
    if (raw / f"kr_{sym}_{real_world.TAG}.json").exists():
        rows = _j.loads((raw / f"kr_{sym}_{real_world.TAG}.json").read_text(encoding="utf-8"))
        return sorted((real_world._iso(r["basDt"]), F(r["clpr"])) for r in rows)
    res = _j.loads((raw / f"us_{sym}_{real_world.TAG}_adjtrue.json").read_text(encoding="utf-8"))["results"]
    return sorted((datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date().isoformat(), F(str(r["c"]))) for r in res)


def big_moves(sym, start, end):
    """보유 기간 start ~ end의 큰 움직임 날과, 판정을 시작할 수 있는 첫날."""
    import statistics
    px = adjusted_closes(sym)
    days = [d for d, _ in px]
    rets = [None] + [float(px[i][1] / px[i - 1][1] - 1) for i in range(1, len(px))]
    judged = days[61] if len(days) > 61 else None
    out = [days[i] for i in range(61, len(px))
           if start <= days[i] <= end and abs(rets[i]) > 2 * statistics.stdev(rets[i - 60:i])]
    return {"dates": out, "judged_from": judged, "not_judged_in_holding": start < judged if judged else True}


def main():
    world = real_world.load()
    m = calculator(world)
    cases = json.loads((HERE / "cases_real.json").read_text(encoding="utf-8"))
    out, bad = {}, []
    print("| 사례 | 종목 | 매매 | 남은 수량 | 경고 | 기대한 경고 |")
    print("|---|---|---:|---:|---|---|")
    for c in cases:
        r = solve(m, c)
        out[c["id"]] = r
        got = [(w["code"], w["trade"]) for w in r["warnings"]]
        want = [(w["code"], w["trade"]) for w in c.get("expect_warnings", [])]
        if c["group"] == "경고" and got != want:
            bad.append(c["id"])
        print(f"| {c['id']} | {c['symbol']} | {len(c['trades'])} | {r['qty_left']} | "
              f"{', '.join(f'{a}({b})' for a, b in got) or '없음'} | {', '.join(f'{a}({b})' for a, b in want) or ''} |")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("{\n" + ",\n".join(f' "{k}": {json.dumps(v, ensure_ascii=False)}' for k, v in out.items()) + "\n}\n",
                   encoding="utf-8")
    print(f"\n정답 {len(out)}개를 {OUT.relative_to(ROOT)}에 씀. 항등식(가격 + 환율 + 교차 = 총손익)은 사례마다 계산기 안에서 확인함")
    print(f"경고 사례의 경고가 기대와 다른 것: {', '.join(bad) if bad else '없음'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
