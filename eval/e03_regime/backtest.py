#!/usr/bin/env python3
"""E03 백테스트: 코스피 일별 지수로 기준선 셋과 H1(이웃), H2(표준화)를 잽니다. 규칙은 docs/ai/experiments/E03-regime.md 3절.

사용(저장소 루트에서 .venv/bin/python eval/e03_regime/backtest.py ...):
  --selftest            미래 바꾸기 시험과 누수 심기 시험(합성 데이터). 실제 시세 없이 돎
  --lock                평가 기준 파일의 sha256과 시각을 criteria_lock.json에 남깁니다(한 번만). git 커밋 대신 쓰는 고정 기록
  --verify              지금 파일이 고정 기록과 같은지 봅니다
  --fetch               ECOS에서 코스피 일별 지수를 받아 data/raw/ecos_kospi/에 둡니다(git 제외, 값은 출력하지 않음).
                        평가 기준이 고정 기록과 같을 때만 받습니다(평가 기준을 먼저 고정, 0009)
  --synthetic N         무작위 세계 N번에 확인 단계를 그대로 돌려 통과 비율(5% 이하여야 함)
  --dev                 개발 구간(앞 60%)에서 판 넷을 재고 아래 규칙으로 하나를 고릅니다
  --confirm 판 --final  확인 구간(뒤 40%)에 그 판을 한 번 돌립니다. 기록이 있으면 다시 돌리지 않습니다
  --sensitivity         판을 다시 고르지 않고, 한 구간의 시험 날짜를 뺀 값을 다시 셉니다(3절 「한 구간이 결과를 지배」).
                        확인 구간은 2026년, 개발 구간은 1997 ~ 1998년을 뺍니다. 결과는 sensitivity_result.json

방법과 기준선의 계산은 판별력 시험(power_sim.py)의 함수를 그대로 씁니다. 수익률은 일별 로그 수익률입니다.
시험 날짜는 h마다 하나(겹치지 않는 창)이고, 비슷한 때 후보는 결과가 t까지 다 나온 날(i + h <= t)만입니다(L2).

판 고르기 규칙(개발 구간 결과를 보기 전에 정함, 2026-11-12):
  1. 창(5, 20)과 대상(범위, 변동성)의 네 조합 가운데 세 기준선을 모두 이긴 조합이 가장 많은 판
  2. 같으면 20일 창 pinball loss의 평균이 작은 판
  확인 구간에는 고른 판 하나만 돌립니다. 시도한 판은 이웃 30, 이웃 50, 이웃 100, 표준화 100의 넷입니다.
"""
import hashlib
import json
import math
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "eval"))

import power_sim as ps  # noqa: E402
from step0_util import get_json, load_key  # noqa: E402

RAW = ROOT / "data" / "raw" / "ecos_kospi"
LOCK = HERE / "criteria_lock.json"
DOC = "docs/ai/experiments/E03-regime.md"
CRITERIA = ["eval/e03_regime/power_sim.py", "eval/e03_regime/power_result.json", "eval/e03_regime/step0_survey.py",
            "eval/e03_regime/step0_result.json", "eval/step0_util.py"]
CODE_AT_LOCK = ["eval/e03_regime/backtest.py", "eval/e03_regime/synthetic_result.json"]
HORIZONS = (5, 20)
CANDIDATES = ["이웃30", "이웃50", "이웃100", "표준화100"]
BASES = ["과거", "최근", "섞음"]
DEV_SHARE = 0.6
COVER = {5: (0.75, 0.85), 20: (0.70, 0.90)}
MIN_EPISODES = 5


def episodes(idx, h):
    """비슷한 날 목록을 겹치지 않는 때로 셉니다(앞 날과 h일 이상 떨어진 날만 새 때)."""
    n, last = 0, -10 ** 9
    for i in sorted(idx):
        if i - last >= h:
            n, last = n + 1, i
    return n


def forecasts(data, t, h, leak=False):
    """날짜 t의 판별 예측. 판 이름 -> (범위 아래, 위, 변동성 예측, 독립 횟수 또는 None).
    leak=True는 누수 심기 시험용으로, 결과가 아직 안 나온 날까지 후보에 넣습니다."""
    sig, ret, fwd, vol = data
    root = math.sqrt(h)
    near = ps.neighbors(sig, ret, t, 0 if leak else h, 100)
    past_idx = range(19, t - h + 1)
    past = [fwd[i] for i in past_idx]
    mean_sig = statistics.fmean(sig[i] for i in past_idx)
    mean_vol = statistics.fmean(vol[i] for i in past_idx)
    s_now = math.exp(sig[t])
    out = {}
    for k in (30, 50, 100):
        sel = near[:k]
        outs = [fwd[i] for i in sel]
        out[f"이웃{k}"] = (ps.quantile(outs, 0.1), ps.quantile(outs, 0.9),
                         statistics.fmean(vol[i] for i in sel), episodes(sel, h))
    zs = [fwd[i] / (math.exp(sig[i]) * root) for i in near]
    out["표준화100"] = (ps.quantile(zs, 0.1) * s_now * root, ps.quantile(zs, 0.9) * s_now * root,
                      sig[t] + statistics.fmean(vol[i] - sig[i] for i in near), episodes(near, h))
    out["과거"] = (ps.quantile(past, 0.1), ps.quantile(past, 0.9), mean_vol, None)
    b1 = ps.Z80 * s_now * root
    out["최근"] = (-b1, b1, sig[t], None)
    b2 = ps.Z80 * math.exp((sig[t] + mean_sig) / 2) * root
    out["섞음"] = (-b2, b2, (sig[t] + mean_vol) / 2, None)
    return out


def segment(r, h, start, end, names, skip=None):
    """[start, end) 안의 시험 날짜(결과도 그 안)마다 손실을 재고 기준선과 비교합니다.
    skip(t)가 참인 시험 날짜는 채점에서 뺍니다(민감도 확인용). 비슷한 때 후보는 그대로입니다."""
    data = ps.prepare(r, h)
    _, _, fwd, vol = data
    dates = [t for t in range(max(start, ps.WARMUP), end - h, h) if not (skip and skip(t))]
    loss = {m: {"range": [], "vol": []} for m in names + BASES}
    hits = dict.fromkeys(names + BASES, 0)
    few = dict.fromkeys(names, 0)
    eps = {m: [] for m in names}
    for t in dates:
        f = forecasts(data, t, h)
        y, yv = fwd[t], vol[t]
        for m in names + BASES:
            lo, hi, v, ep = f[m]
            loss[m]["range"].append(ps.pinball(y, lo, hi))
            loss[m]["vol"].append(abs(yv - v))
            hits[m] += lo <= y <= hi
            if m in few:
                few[m] += ep < MIN_EPISODES
                eps[m].append(ep)
    out = {"h": h, "dates": len(dates)}
    for m in names + BASES:
        out[m] = {"pinball": statistics.fmean(loss[m]["range"]), "vol_err": statistics.fmean(loss[m]["vol"]),
                  "coverage": hits[m] / len(dates)}
        if m in names:
            beats = {tg: {b: ps.passes([a - c for a, c in zip(loss[m][tg], loss[b][tg])]) for b in BASES}
                     for tg in ("range", "vol")}
            out[m].update(beats=beats, all_range=all(beats["range"].values()), all_vol=all(beats["vol"].values()),
                          too_few=few[m] / len(dates), median_episodes=statistics.median(eps[m]))
    return out


def kospi():
    files = sorted(RAW.glob("kospi_*.json"))
    if not files:
        sys.exit("코스피 원본이 없습니다. --fetch를 먼저 돌리세요")
    rows = json.loads(files[-1].read_text(encoding="utf-8"))
    days = [x["TIME"] for x in rows]
    px = [float(x["DATA_VALUE"]) for x in rows]
    return days, [math.log(b / a) for a, b in zip(px, px[1:])], files[-1].name


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def section3(text):
    """E03 문서의 3절(평가 기준). 상태 줄과 4절, 5절을 고쳐도 이 값은 그대로여야 합니다."""
    return text[text.index("\n## 3. ") + 1:text.index("\n## 4. ")]


def digests():
    doc = (ROOT / DOC).read_text(encoding="utf-8")
    crit = {f"{DOC} 3절": _sha(section3(doc).encode("utf-8"))}
    crit.update({f: _sha((ROOT / f).read_bytes()) for f in CRITERIA})
    return crit, {f: _sha((ROOT / f).read_bytes()) for f in CODE_AT_LOCK}, _sha(doc.encode("utf-8"))


def lock():
    if LOCK.exists():
        sys.exit("고정 기록이 이미 있습니다. 다시 만들지 않습니다")
    crit, code, whole = digests()
    LOCK.write_text(json.dumps({
        "locked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "why": "git 커밋 대신 평가 기준을 고정한 기록(11/12 결정). 이 뒤에 코스피 값을 처음 받음",
        "criteria_sha256": crit, "code_at_lock_sha256": code, "doc_whole_sha256_at_lock": whole,
        "check": "python3 eval/e03_regime/backtest.py --verify (3절은 '## 3. '부터 '## 4. ' 앞까지의 UTF-8 바이트)",
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"고정 기록: {LOCK.relative_to(ROOT)}")
    return verify()


def verify():
    if not LOCK.exists():
        print("고정 기록이 없습니다")
        return 1
    rec = json.loads(LOCK.read_text(encoding="utf-8"))
    crit, code, _ = digests()
    bad = [k for k, v in rec["criteria_sha256"].items() if crit.get(k) != v]
    moved = [k for k, v in rec["code_at_lock_sha256"].items() if code.get(k) != v]
    print(f"평가 기준 {len(crit) - len(bad)}/{len(crit)} 같음" + (f", 다름: {bad}" if bad else "")
          + f". 고정 때의 코드 {len(code) - len(moved)}/{len(code)} 같음" + (f", 바뀜: {moved}" if moved else ""))
    return 1 if bad else 0


def fetch():
    if verify():
        sys.exit("평가 기준이 고정 기록과 다르거나 기록이 없습니다. 고정한 뒤에 받습니다(0009)")
    key = load_key("ECOS_API_KEY")
    end = date.today().strftime("%Y%m%d")
    code, data = get_json(f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/10000/802Y001/D/19950103/{end}/0001000")
    rows = (data or {}).get("StatisticSearch", {}).get("row")
    if code != 200 or not rows:
        sys.exit(f"ECOS 조회 실패: HTTP {code}, {(data or {}).get('RESULT', {}).get('MESSAGE', '')}")
    RAW.mkdir(parents=True, exist_ok=True)
    keep = [{"TIME": x["TIME"], "DATA_VALUE": x["DATA_VALUE"]} for x in rows if x.get("DATA_VALUE")]
    path = RAW / f"kospi_{keep[0]['TIME']}_{keep[-1]['TIME']}.json"
    path.write_text(json.dumps(keep), encoding="utf-8")
    print(f"받음: {len(keep)}행, {keep[0]['TIME']} ~ {keep[-1]['TIME']}, {path.relative_to(ROOT)}(값은 출력하지 않음)")


def selftest():
    """미래 바꾸기 시험과 누수 심기 시험. 합성 무작위 세계에서, 날짜 t 뒤의 값을 모두 바꿔도 t의 예측이 같아야 합니다."""
    rng = random.Random(20261112)
    r = ps.series("무작위", 1500, rng)[0]
    bad = []
    for h in HORIZONS:
        for leak in (False, True):
            changed = 0
            dates = list(range(ps.WARMUP, len(r) - h, 40))
            for t in dates:
                r2 = r[:t + 1] + [rng.gauss(0, 0.02) for _ in range(len(r) - t - 1)]
                a = forecasts(ps.prepare(r, h), t, h, leak)
                b = forecasts(ps.prepare(r2, h), t, h, leak)
                changed += a != b
            print(f"h={h} {'누수 심은 판' if leak else '실제 판'}: 시험 날짜 {len(dates)}개 가운데 미래를 바꾸자 예측이 바뀐 날 {changed}개")
            if (changed > 0) != leak:
                bad.append((h, leak, changed))
    print("통과" if not bad else f"실패: {bad}")
    return 0 if not bad else 1


def _synthetic_one(seed):
    rng = random.Random(seed)
    n = 8000
    r = ps.series("무작위", n, rng)[0]
    split = int(n * DEV_SHARE)
    return {h: segment(r, h, split, n, CANDIDATES) for h in HORIZONS}


def synthetic(reps):
    with ProcessPoolExecutor(10) as pool:
        res = list(pool.map(_synthetic_one, [20261112 + i for i in range(reps)]))
    out = {}
    for m in CANDIDATES:
        out[m] = {f"{tg}_{h}": sum(x[h][m][f"all_{tg}"] for x in res) / reps for h in HORIZONS for tg in ("range", "vol")}
    print("| 판 | " + " | ".join(f"{tg} {h}일" for h in HORIZONS for tg in ("범위", "변동성")) + " |")
    print("|---|---|---|---|---|")
    for m, v in out.items():
        print(f"| {m} | " + " | ".join(f"{v[f'{tg}_{h}']:.0%}" for h in HORIZONS for tg in ("range", "vol")) + " |")
    worst = max(x for v in out.values() for x in v.values())
    print(f"\n무작위 세계 {reps}번, 확인 단계(뒤 40%)를 그대로 돌려 세 기준선을 모두 이긴 비율. 가장 높은 값 {worst:.0%}(통과선 5% 이하)")
    (HERE / "synthetic_result.json").write_text(json.dumps({"reps": reps, "n": 8000, "pass_rate": out}, ensure_ascii=False,
                                                            indent=1) + "\n", encoding="utf-8")
    return 0 if worst <= 0.05 else 1


def pick(res):
    score = lambda m: (sum(res[h][m][f"all_{tg}"] for h in HORIZONS for tg in ("range", "vol")), -res[20][m]["pinball"])
    return max(CANDIDATES, key=score)


def report(res, names, title):
    print(f"\n{title}")
    for h in HORIZONS:
        r = res[h]
        print(f"\n창 {h}일, 시험 날짜 {r['dates']}개 (적중률 통과선 {COVER[h][0]:.0%} ~ {COVER[h][1]:.0%})")
        print("| 판 | pinball | 변동성 오차 | 80% 적중률 | 범위: 과거, 최근, 섞음 | 변동성: 과거, 최근, 섞음 | 5번 미만 |")
        print("|---|---|---|---|---|---|---|")
        for m in names + BASES:
            x = r[m]
            b = x.get("beats")
            mark = lambda d: ", ".join("이김" if d[k] else "못 이김" for k in BASES)
            print(f"| {m} | {x['pinball']:.5f} | {x['vol_err']:.4f} | {x['coverage']:.1%} | "
                  f"{mark(b['range']) if b else '-'} | {mark(b['vol']) if b else '-'} | "
                  f"{format(x['too_few'], '.0%') if b else '-'} |")


def run(mode):
    days, r, src = kospi()
    split = int(len(r) * DEV_SHARE)
    meta = {"source": src, "days": len(days), "first": days[0], "last": days[-1],
            "dev": [days[1], days[split]], "confirm": [days[split + 1], days[-1]]}
    if mode == "dev":
        res = {h: segment(r, h, 0, split, CANDIDATES) for h in HORIZONS}
        chosen = pick(res)
        report(res, CANDIDATES, f"개발 구간 {meta['dev'][0]} ~ {meta['dev'][1]}, 판 {len(CANDIDATES)}개")
        print(f"\n고른 판: {chosen}(규칙: 세 기준선을 모두 이긴 조합 수, 같으면 20일 pinball)")
        out = {"checked": date.today().isoformat(), "split": "dev", **meta, "tried": CANDIDATES, "chosen": chosen,
               "result": {str(h): v for h, v in res.items()}}
        (HERE / "dev_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return 0
    name = sys.argv[sys.argv.index("--confirm") + 1]
    target = HERE / "confirm_result.json"
    if "--final" not in sys.argv or name not in CANDIDATES:
        sys.exit("확인 구간은 --confirm 판이름 --final로 한 번만 돌립니다")
    if target.exists():
        sys.exit("확인 구간 기록이 이미 있습니다. 다시 돌리지 않습니다")
    if verify():
        sys.exit("평가 기준이 고정 기록과 다릅니다")
    t0 = time.perf_counter()
    res = {h: segment(r, h, split, len(r), [name]) for h in HORIZONS}
    report(res, [name], f"확인 구간 {meta['confirm'][0]} ~ {meta['confirm'][1]}, 판 {name}(한 번)")
    verdict = {}
    for h in HORIZONS:
        x = res[h][name]
        lo, hi = COVER[h]
        verdict[str(h)] = {"beats_all_range": x["all_range"], "beats_all_vol": x["all_vol"],
                           "coverage_ok": lo <= x["coverage"] <= hi,
                           "best_base_by_pinball": min(BASES, key=lambda b: res[h][b]["pinball"]),
                           "base_coverage_ok": {b: lo <= res[h][b]["coverage"] <= hi for b in BASES}}
    print("\n판정:", json.dumps(verdict, ensure_ascii=False))
    out = {"checked": date.today().isoformat(), "split": "confirm", **meta, "method": name, "verdict": verdict,
           "seconds": round(time.perf_counter() - t0, 1), "result": {str(h): v for h, v in res.items()}}
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


def sensitivity():
    if verify():
        sys.exit("평가 기준이 고정 기록과 다릅니다")
    days, r, src = kospi()
    split = int(len(r) * DEV_SHARE)
    chosen = json.loads((HERE / "confirm_result.json").read_text(encoding="utf-8"))["method"]
    # r[t]는 days[t + 1]의 수익률. 예측 시점은 days[t + 1] 장 마감, 결과 창은 days[t + 2] ~ days[t + h + 1]
    touches = lambda years, h: (lambda t: any(days[i][:4] in years for i in range(t + 1, min(t + h + 2, len(days)))))
    out = {"checked": date.today().isoformat(), "source": src, "rule": "판을 다시 고르지 않음. 그 해에 예측 시점이나 결과 창이 걸친 시험 날짜만 뺌"}
    same = {h: segment(r, h, split, len(r), [chosen]) for h in HORIZONS}
    saved = json.loads((HERE / "confirm_result.json").read_text(encoding="utf-8"))["result"]
    out["confirm_recount_same"] = all(same[h][chosen] == saved[str(h)][chosen] for h in HORIZONS)
    print(f"확인 구간을 빼는 것 없이 다시 센 값이 기록과 같은가: {out['confirm_recount_same']}")
    conf = {h: segment(r, h, split, len(r), [chosen], touches({"2026"}, h)) for h in HORIZONS}
    report(conf, [chosen], f"확인 구간에서 2026년을 뺌, 판 {chosen}")
    dev = {h: segment(r, h, 0, split, CANDIDATES, touches({"1997", "1998"}, h)) for h in HORIZONS}
    report(dev, CANDIDATES, "개발 구간에서 1997 ~ 1998년을 뺌, 판 넷")
    print(f"\n1997 ~ 1998년을 빼고 규칙대로 고르면: {pick(dev)}(실제로 고른 판: {chosen}. 판은 다시 고르지 않음)")
    out["confirm_without_2026"] = {str(h): v for h, v in conf.items()}
    out["dev_without_1997_1998"] = {str(h): v for h, v in dev.items()}
    out["dev_pick_without_1997_1998"] = pick(dev)
    (HERE / "sensitivity_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    a = sys.argv
    if "--selftest" in a:
        sys.exit(selftest())
    if "--lock" in a:
        sys.exit(lock())
    if "--verify" in a:
        sys.exit(verify())
    if "--fetch" in a:
        sys.exit(fetch())
    if "--synthetic" in a:
        sys.exit(synthetic(int(a[a.index("--synthetic") + 1])))
    if "--sensitivity" in a:
        sys.exit(sensitivity())
    if "--dev" in a:
        sys.exit(run("dev"))
    if "--confirm" in a:
        sys.exit(run("confirm"))
    print(__doc__)
