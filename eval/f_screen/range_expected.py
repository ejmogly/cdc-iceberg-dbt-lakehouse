#!/usr/bin/env python3
"""F 화면 8번 칸(앞으로의 범위)의 정답을 화면과 따로 냅니다(docs/ai/F-screen.md 4절 「범위 칸」, 평가셋 「범위」).

사용(저장소 루트에서):
  .venv/bin/python eval/f_screen/range_expected.py              정답을 data/generated/f_screen/expected_range.json에 쓰고 표를 냅니다
  .venv/bin/python eval/f_screen/range_expected.py --selftest   미래 바꾸기 시험과 누수 심기 시험

화면 코드보다 먼저 두는 독립 정답입니다. 원본(data/raw/)을 직접 읽고, 적재와 내보내기를 거치지 않습니다.
화면 코드는 이 파일을 가져다 쓰지 않고, 이 파일도 eval/e03_regime/backtest.py를 가져다 쓰지 않습니다(정의만 다시 구현).

정의(E03 3절의 기준선 「최근」, backtest.py의 forecasts()와 power_sim.py의 prepare()를 따름):
- 대상: 현지 통화 가격의 일별 로그 수익률. 환율과 분배금은 넣지 않습니다.
  미국은 Massive 수정한 가격(adjtrue)의 종가 c이고, results의 t(UTC 밀리초)를 UTC 날짜로 바꿉니다(미국 동부 자정이라 거래일과 같음).
  국내는 공공데이터포털의 basDt와 종가 clpr입니다.
- 「최근」 기준선: 날짜 p의 장 마감까지 아는 마지막 일별 로그 수익률 20개(가격 21개, p - 20 ~ p)로
  s = sqrt(합(r x r) / 20) + 1e-9 (평균을 빼지 않고 20으로 나눔. 1e-9는 backtest.py가 로그를 보호하려 더한 값).
  앞으로 20거래일 로그 수익률의 80% 범위(10%, 90% 지점)는 -1.2816 x s x sqrt(20) ~ +1.2816 x s x sqrt(20).
- 기준일 t: 종목마다 둘. 그 종목 데이터의 마지막 거래일, 그 날에서 365일 앞 이하의 마지막 거래일.
- 적중률: 기준일 t에서 20거래일씩 거슬러 간 예측 날짜 p = t - 20, t - 40, ...(겹치지 않는 창, 마지막 창의 끝이 t).
  p 앞에 수익률 20개가 있어야 셉니다(p >= 20). 실제 값은 p 종가에서 20거래일 뒤 종가까지의 로그 수익률이고,
  범위 안(경계 포함)이면 맞음. 70 ~ 90% 밖이면 알림(E03 「참고 적중률」, 경계 포함).
- 가격은 출력하지도 저장하지도 않습니다(재배포 금지). 범위, n, 맞은 수만 냅니다.
"""
import argparse
import hashlib
import json
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "daily_prices"
INSTRUMENTS = ROOT / "loaders" / "instruments.csv"
OUT = ROOT / "data" / "generated" / "f_screen" / "expected_range.json"
TAG = "20241113_20261112"

H = 20  # 앞으로 볼 거래일 수
LOOKBACK = 20  # 「최근」 기준선이 보는 일별 수익률 수
Z80 = 1.2816  # power_sim.py의 Z80(표준정규 90% 지점)
EPS = 1e-9  # power_sim.prepare()의 로그 보호값
COVER = (0.70, 0.90)  # 20일 참고 적중률 알림선(E03 3절)
MIN_N = 20  # 이보다 작은 n에서는 70 ~ 90% 알림 대신 표본 문구
YEAR_BACK = 365  # 둘째 기준일: 마지막 날에서 365일 앞 이하의 마지막 거래일


def instruments():
    rows = INSTRUMENTS.read_text(encoding="utf-8").splitlines()
    head = rows[0].split(",")
    return [dict(zip(head, r.split(","))) for r in rows[1:] if r]


def source_path(sym, market):
    if market == "KRX":
        return RAW / f"kr_{sym}_{TAG}.json"
    return RAW / f"us_{sym}_{TAG}_adjtrue.json"


def load(sym, market):
    """원본에서 (거래일 목록, 종가 목록, 파일 경로). 날짜 오름차순이고 중복과 0 이하 값이 없어야 합니다."""
    path = source_path(sym, market)
    if not path.exists():
        sys.exit(f"원본이 없습니다: {path.relative_to(ROOT)}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if market == "KRX":
        pairs = [(datetime.strptime(r["basDt"], "%Y%m%d").date(), float(r["clpr"])) for r in data]
    else:
        if data.get("adjusted") is not True or data.get("ticker") != sym:
            sys.exit(f"{sym}: 수정한 가격 원본이 아닙니다({path.name})")
        pairs = [(datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date(), float(r["c"]))
                 for r in data.get("results") or []]
    pairs.sort()
    days = [d for d, _ in pairs]
    px = [c for _, c in pairs]
    if len(set(days)) != len(days):
        sys.exit(f"{sym}: 같은 날짜가 두 번 있습니다")
    if any(not (c > 0) for c in px):
        sys.exit(f"{sym}: 0 이하이거나 빈 종가가 있습니다")
    if len(px) < LOOKBACK + H + 1:
        sys.exit(f"{sym}: 행이 {len(px)}개뿐이라 창 하나도 셀 수 없습니다")
    return days, px, path


def band(px, p):
    """날짜 p(가격 순번)의 「최근」 80% 범위(로그 수익률의 아래, 위). px[p - 20] ~ px[p]만 읽습니다."""
    if p < LOOKBACK:
        return None
    rs = [math.log(px[i] / px[i - 1]) for i in range(p - LOOKBACK + 1, p + 1)]
    s = math.sqrt(sum(x * x for x in rs) / LOOKBACK) + EPS
    half = Z80 * s * math.sqrt(H)
    return -half, half


def band_leak(px, p):
    """누수 심기 시험용. 수익률 창을 하루 밀어 p 다음 날의 수익률까지 씁니다(흔한 하루 어긋남 실수)."""
    if p < LOOKBACK - 1:
        return None
    rs = [math.log(px[i] / px[i - 1]) for i in range(p - LOOKBACK + 2, p + 2)]
    s = math.sqrt(sum(x * x for x in rs) / LOOKBACK) + EPS
    half = Z80 * s * math.sqrt(H)
    return -half, half


def windows(px, t, band_fn=band):
    """기준일 t에서 20거래일씩 거슬러 간 창. 오래된 순으로 (예측 날짜 순번, 아래, 위, 실제, 맞음).
    실제 값은 px[p] ~ px[p + 20]이라 t 뒤 자료는 쓰지 않습니다."""
    out = []
    p = t - H
    while p >= LOOKBACK:
        lo, hi = band_fn(px, p)
        y = math.log(px[p + H] / px[p])
        out.append((p, lo, hi, y, lo <= y <= hi))
        p -= H
    return out[::-1]


def base_dates(days):
    """(이름, 가격 순번) 둘: 마지막 거래일과, 그 날에서 365일 앞 이하의 마지막 거래일."""
    last = len(days) - 1
    target = days[last] - timedelta(days=YEAR_BACK)
    before = [i for i, d in enumerate(days) if d <= target]
    if not before:
        sys.exit(f"{days[last]}에서 {YEAR_BACK}일 앞 이하의 거래일이 없습니다")
    return [("마지막 날", last), ("1년 전", before[-1])]


def pct(x):
    """로그 수익률을 가격 변화 %로."""
    return (math.exp(x) - 1) * 100


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    rows, sources = [], {}
    for ins in instruments():
        sym, market = ins["symbol"], ins["market"]
        days, px, path = load(sym, market)
        sources[sym] = {"file": str(path.relative_to(ROOT)), "sha256": sha256(path), "rows": len(px),
                        "first": days[0].isoformat(), "last": days[-1].isoformat()}
        for base, t in base_dates(days):
            lo, hi = band(px, t)
            ws = windows(px, t)
            hits = sum(w[4] for w in ws)
            rate = hits / len(ws) if ws else None
            rows.append({
                "symbol": sym, "name": ins["name"], "market": market, "group": ins["group"],
                "base": base, "date": days[t].isoformat(),
                "lo_log": lo, "hi_log": hi, "lo_pct": pct(lo), "hi_pct": pct(hi),
                "n": len(ws), "hits": hits, "hit_rate": rate,
                "outside_70_90": rate is None or not (COVER[0] <= rate <= COVER[1]),
                # 화면에 보일 것(F-screen.md 2절 8번 칸): n이 20보다 작으면 알림 대신 표본 문구
                "display": "표본 문구" if len(ws) < MIN_N else (
                    "알림" if rate is None or not (COVER[0] <= rate <= COVER[1]) else "없음"),
                "windows": [{"date": days[p].isoformat(), "lo_log": a, "hi_log": b, "actual_log": y, "hit": ok}
                            for p, a, b, y, ok in ws],
            })
    return {
        "what": "F 화면 8번 칸의 독립 정답(docs/ai/F-screen.md 4절 「범위 칸」). 가격은 넣지 않음",
        "definition": {
            "target": "현지 통화 가격의 일별 로그 수익률. 미국은 Massive 수정한 가격(adjtrue) 종가, 국내는 공공데이터포털 종가",
            "baseline": "E03 「최근」: s = sqrt(합(r x r) / 20) + 1e-9, 마지막 수익률 20개. 범위 = -z x s x sqrt(h) ~ +z x s x sqrt(h)",
            "z": Z80, "h": H, "lookback_returns": LOOKBACK, "eps": EPS,
            "base_dates": "마지막 거래일, 그 날에서 365일 앞 이하의 마지막 거래일",
            "windows": "기준일 t에서 20거래일씩 거슬러 간 예측 날짜(t - 20, t - 40, ...), 예측 날짜 앞에 수익률 20개가 있는 창만. 경계 포함이면 맞음",
            "alert": "적중률이 70 ~ 90% 밖(경계는 안으로 봄)이면 outside_70_90 = true",
            "display": "n < 20이면 「표본 문구」, 아니면 70 ~ 90% 밖일 때 「알림」, 안이면 「없음」(F-screen.md 2절 8번 칸)",
            "units": "lo_log, hi_log, actual_log는 로그 수익률. lo_pct, hi_pct는 가격 변화 %((exp(x) - 1) x 100). hit_rate는 0 ~ 1",
        },
        "sources": sources,
        "rows": rows,
    }


def main():
    res = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    rows = res["rows"]
    print("| 종목 | 기준일 | 날짜 | 80% 범위(20거래일) | n | 맞음 | 적중률 | 70 ~ 90% 밖 |")
    print("|---|---|---|---|---:|---:|---:|---|")
    for r in rows:
        rate = f"{r['hit_rate']:.1%}" if r["hit_rate"] is not None else "-"
        print(f"| {r['symbol']} | {r['base']} | {r['date']} | {r['lo_pct']:+.2f}% ~ {r['hi_pct']:+.2f}% | "
              f"{r['n']} | {r['hits']} | {rate} | {'알림' if r['outside_70_90'] else ''} |")
    print()
    for base in ("마지막 날", "1년 전"):
        part = [r for r in rows if r["base"] == base]
        n, hits = sum(r["n"] for r in part), sum(r["hits"] for r in part)
        out = [r["symbol"] for r in part if r["outside_70_90"]]
        ns = sorted({r["n"] for r in part})
        print(f"{base}: 종목 {len(part)}개, 종목별 n {ns[0]} ~ {ns[-1]}, 합친 적중률 {hits}/{n} = {hits / n:.1%}"
              f"(같은 시기 창이 종목끼리 겹쳐 독립이 아님, 참고). 70 ~ 90% 밖 {len(out)}개: {', '.join(out) if out else '없음'}")
    print(f"\n정답: {OUT.relative_to(ROOT)}(git 제외), sha256 {sha256(OUT)}")
    print("가격 값은 출력하지 않았습니다(재배포 금지).")
    return 0


def perturb(px, p, mode, rng):
    """p 뒤의 가격을 모두 바꾼 사본. 1.5배는 p와 p + 1 사이 수익률만 바뀌고, 무작위는 그 뒤 수익률도 모두 바뀝니다."""
    if mode == "1.5배":
        return px[:p + 1] + [c * 1.5 for c in px[p + 1:]]
    out = px[:p + 1]
    for _ in range(len(px) - p - 1):
        out.append(out[-1] * math.exp(rng.gauss(0, 0.03)))
    return out


def selftest():
    """미래 바꾸기 시험과 누수 심기 시험. 실제 원본을 쓰지만 가격과 범위 값은 출력하지 않습니다.
    시험 날짜는 종목마다 두 기준일과, 두 기준일의 적중률 창 예측 날짜 전부입니다.
    마지막 거래일은 뒤 자료가 없어 늘 같으므로 세지 않습니다(F-screen.md 4절)."""
    rng = random.Random(20261113)
    modes = ("1.5배", "무작위")
    total = {(name, m): [0, 0] for name in ("실제 판", "누수 심은 판") for m in modes}
    hit_total = {m: [0, 0] for m in modes}
    skipped, missed_leak, bad = 0, [], []
    for ins in instruments():
        sym, market = ins["symbol"], ins["market"]
        days, px, _ = load(sym, market)
        bases = base_dates(days)
        points = {t for _, t in bases} | {w[0] for _, t in bases for w in windows(px, t)}
        skipped += sum(p >= len(px) - 1 for p in points)
        points = sorted(p for p in points if p < len(px) - 1)
        for name, fn in (("실제 판", band), ("누수 심은 판", band_leak)):
            for m in modes:
                changed = 0
                for p in points:
                    changed += fn(px, p) != fn(perturb(px, p, m, rng), p)
                total[(name, m)][0] += len(points)
                total[(name, m)][1] += changed
                if name == "실제 판" and changed:
                    bad.append(f"{sym} {m} {changed}개")
                if name == "누수 심은 판" and not changed:
                    missed_leak.append(f"{sym} {m}")
        # 기준일의 적중률(창 전체)도 기준일 뒤 값과 무관해야 합니다. 마지막 날은 뒤 자료가 없어 1년 전만 봅니다
        for _, t in bases:
            if t >= len(px) - 1:
                continue
            for m in modes:
                hit_total[m][0] += 1
                if windows(px, t) != windows(perturb(px, t, m, rng), t):
                    hit_total[m][1] += 1
                    bad.append(f"{sym} {m} 기준일 {days[t]} 적중률")
    print("| 판 | 바꾸기 | 시험 날짜 | 범위가 바뀐 날 |")
    print("|---|---|---:|---:|")
    for (name, m), (n, c) in total.items():
        print(f"| {name} | {m} | {n} | {c} |")
    print(f"\n마지막 거래일 {skipped}개는 뒤 자료가 없어 세지 않았습니다(늘 같음).")
    for m in modes:
        print(f"1년 전 기준일의 창 전체(범위, 실제 값, 맞음)가 바뀐 종목: {hit_total[m][1]}/{hit_total[m][0]}({m})")
    print(f"누수를 놓친 종목: {', '.join(missed_leak) if missed_leak else '없음'}")
    ok = not bad and not missed_leak
    print("통과" if ok else f"실패: {', '.join(bad + missed_leak)}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="F 화면 8번 칸(앞으로의 범위)의 독립 정답")
    ap.add_argument("--selftest", action="store_true", help="미래 바꾸기 시험과 누수 심기 시험")
    sys.exit(selftest() if ap.parse_args().selftest else main())
