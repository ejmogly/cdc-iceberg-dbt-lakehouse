#!/usr/bin/env python3
"""E03 통과선을 정하기 전에, 표본 크기별로 그 시험이 무엇을 가려낼 수 있는지 잰다.

실제 시세는 쓰지 않는다. 답을 아는 합성 세계 셋을 만들어 평가 절차를 돌린다.
  무작위  날마다 독립. 찾을 것이 없으므로 통과하면 오탐이다.
  군집    변동성만 이어진다(GARCH).
  국면    오름과 내림 국면이 이어진다. 비슷한 때 찾기가 실제로 값을 할 수 있는 세계다.

방법 셋을 기준선 셋과 비교한다.
  이웃     비슷한 날 30개의 h일 뒤 수익률을 그대로 쓴다.
  표준화   비슷한 날 100개의 결과를 그때 변동성으로 나눈 뒤, 오늘 변동성을 곱한다.
  정답     세계의 규칙을 아는 방법. 이것도 통과하지 못하면 시험의 표본이 모자란 것이다.
  기준선   과거 전체 분포, 최근 20일 변동성, 둘을 반씩 섞은 변동성.

사용: python3 power_sim.py [--episodes]   (표준 라이브러리만 씀, 결과는 power_result.json)
"""
import heapq
import json
import math
import random
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
Z80 = 1.2816
K_NAIVE = 30
K_WIDE = 100
WARMUP = 250
DAILY = 0.01  # 하루 표준편차 1%로 맞춘 값. 통과 비율은 이 값에 거의 영향받지 않는다.
GARCH_A, GARCH_B = 0.10, 0.85
SWITCH = 0.01  # 국면이 하루에 바뀔 확률. 평균 100거래일 이어진다.
BULL, BEAR = (0.08, 0.65), (-0.16, 1.25)
MIX = math.sqrt(0.5)  # 여러 종목일 때 함께 움직이는 부분과 종목 고유 부분의 비중

SIZES = [
    {"name": "ETF 12개, 2년, 20일", "n": 500, "s": 12, "h": 20, "reps": 300},
    {"name": "ETF 12개, 2년, 5일", "n": 500, "s": 12, "h": 5, "reps": 200},
    {"name": "한 시계열, 10년, 20일", "n": 2500, "s": 1, "h": 20, "reps": 200},
    {"name": "한 시계열, 10년, 5일", "n": 2500, "s": 1, "h": 5, "reps": 100},
    {"name": "한 시계열, 30년, 20일", "n": 7500, "s": 1, "h": 20, "reps": 60},
    {"name": "한 시계열, 30년, 5일", "n": 7500, "s": 1, "h": 5, "reps": 30},
]
WORLDS = ["무작위", "군집", "국면"]
METHODS = ["이웃", "표준화", "정답"]
BASES = ["과거", "최근", "섞음"]


def t5(rng):
    """분산 1로 맞춘 자유도 5의 t분포."""
    g = rng.gauss(0, 1)
    c = sum(rng.gauss(0, 1) ** 2 for _ in range(5))
    return g / math.sqrt(c / 5) / math.sqrt(5 / 3)


def series(world, n, rng):
    """일별 수익률과, 그날 끝에 세계가 알고 있는 상태(군집은 다음 날 분산, 국면은 현재 국면)."""
    out, info = [], []
    if world == "무작위":
        return [t5(rng) for _ in range(n)], [None] * n
    if world == "군집":
        var = 1.0
        for _ in range(n):
            r = math.sqrt(var) * t5(rng)
            var = (1 - GARCH_A - GARCH_B) + GARCH_A * r * r + GARCH_B * var
            out.append(r)
            info.append(var)
        return out, info
    bull = True
    for _ in range(n):
        if rng.random() < SWITCH:
            bull = not bull
        mu, sd = BULL if bull else BEAR
        out.append(mu + sd * t5(rng))
        info.append(bull)
    return out, info


def panel(world, n, s, rng):
    """s개 종목. 종목끼리 같은 날 함께 움직이는 부분을 둔다. 상태는 공통 부분의 것이다."""
    f, info = series(world, n, rng)
    if s == 1:
        return [[x * DAILY for x in f]], info
    out = []
    for _ in range(s):
        e = [t5(rng) for _ in range(n)]
        out.append([(MIX * a + MIX * b) * DAILY for a, b in zip(f, e)])
    return out, info


def regime_table(h, s, rng, draws=6000):
    """국면 세계에서 현재 국면별 h일 뒤 결과의 참값을 모의실험으로 구한다."""
    table = {}
    for start in (True, False):
        sums, vols = [], []
        for _ in range(draws):
            bull = start
            x = v = 0.0
            for _ in range(h):
                if rng.random() < SWITCH:
                    bull = not bull
                mu, sd = BULL if bull else BEAR
                r = mu + sd * t5(rng)
                if s > 1:
                    r = MIX * r + MIX * t5(rng)
                x += r
                v += r * r
            sums.append(x * DAILY)
            vols.append(math.log(math.sqrt(v / h) * DAILY + 1e-9))
        table[start] = (quantile(sums, 0.1), quantile(sums, 0.9), statistics.fmean(vols))
    return table


def oracle_band(h, s, rng, draws=20000):
    """무작위 세계에서 h일 합의 참 80% 범위(대칭)."""
    xs = []
    for _ in range(draws):
        x = 0.0
        for _ in range(h):
            x += t5(rng) if s == 1 else MIX * t5(rng) + MIX * t5(rng)
        xs.append(abs(x) * DAILY)
    return quantile(xs, 0.8)


def garch_total(var1, h, s):
    """군집 세계에서 앞으로 h일 분산 합의 기댓값."""
    p = GARCH_A + GARCH_B
    total = sum(1 + p ** k * (var1 - 1) for k in range(h))
    return total if s == 1 else 0.5 * total + 0.5 * h


def prepare(r, h):
    """날짜 i까지 아는 값(sig, ret)과 i 뒤 h일의 결과(fwd, vol)."""
    n = len(r)
    pre = [0.0]
    sq = [0.0]
    for x in r:
        pre.append(pre[-1] + x)
        sq.append(sq[-1] + x * x)
    sig, ret, fwd, vol = [math.nan] * n, [math.nan] * n, [math.nan] * n, [math.nan] * n
    for i in range(19, n):
        sig[i] = math.log(math.sqrt((sq[i + 1] - sq[i - 19]) / 20) + 1e-9)
        ret[i] = pre[i + 1] - pre[i - 19]
        if i + h < n:
            fwd[i] = pre[i + h + 1] - pre[i + 1]
            vol[i] = math.log(math.sqrt((sq[i + h + 1] - sq[i + 1]) / h) + 1e-9)
    return sig, ret, fwd, vol


def quantile(xs, q):
    s = sorted(xs)
    p = q * (len(s) - 1)
    lo = int(p)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (p - lo)


def pinball(y, lo, hi):
    def one(q, f):
        return (q * (y - f)) if y >= f else ((1 - q) * (f - y))
    return (one(0.1, lo) + one(0.9, hi)) / 2


def neighbors(sig, ret, t, h, k):
    """t까지 결과가 다 나온 날(i + h <= t)만 후보로 삼아 가까운 k개를 가까운 순서로 고른다."""
    lib = range(19, t - h + 1)
    ss = [sig[i] for i in lib]
    rr = [ret[i] for i in lib]
    ms, sds = statistics.fmean(ss), statistics.pstdev(ss) or 1.0
    mr, sdr = statistics.fmean(rr), statistics.pstdev(rr) or 1.0
    a, b = (sig[t] - ms) / sds, (ret[t] - mr) / sdr
    near = heapq.nsmallest(k, ((((x - ms) / sds - a) ** 2 + ((y - mr) / sdr - b) ** 2, i)
                               for i, x, y in zip(lib, ss, rr)))
    return [i for _, i in near]


def passes(diffs):
    """날짜별 차이(방법 - 기준선)가 음수 쪽으로 유의한가. 한쪽 5%, t 근사."""
    if len(diffs) < 3:
        return False
    m = statistics.fmean(diffs)
    sd = statistics.stdev(diffs)
    if sd == 0:
        return False
    return m / (sd / math.sqrt(len(diffs))) < -(1.645 + 1.7 / (len(diffs) - 1))


def one_rep(world, cfg, oracle, table, rng):
    n, s, h = cfg["n"], cfg["s"], cfg["h"]
    series_all, info = panel(world, n, s, rng)
    data = [prepare(r, h) for r in series_all]
    dates = list(range(WARMUP, n - h, h))
    root = math.sqrt(h)
    # 날짜별 손실: (대상, 방법, 기준선) -> [차이]
    diffs = {(tg, m, b): [] for tg in ("range", "vol") for m in METHODS for b in BASES}
    hits = dict.fromkeys(METHODS + BASES, 0)
    total = 0
    for t in dates:
        day = {key: [] for key in diffs}
        for sig, ret, fwd, vol in data:
            y, yv = fwd[t], vol[t]
            near = neighbors(sig, ret, t, h, K_WIDE)
            past_idx = range(19, t - h + 1)
            past = [fwd[i] for i in past_idx]
            mean_sig = statistics.fmean(sig[i] for i in past_idx)
            mean_vol = statistics.fmean(vol[i] for i in past_idx)
            s_now = math.exp(sig[t])

            ranges = {}
            preds = {}
            outs = [fwd[i] for i in near[:K_NAIVE]]
            ranges["이웃"] = (quantile(outs, 0.1), quantile(outs, 0.9))
            preds["이웃"] = statistics.fmean(vol[i] for i in near[:K_NAIVE])
            zs = [fwd[i] / (math.exp(sig[i]) * root) for i in near]
            ranges["표준화"] = (quantile(zs, 0.1) * s_now * root, quantile(zs, 0.9) * s_now * root)
            preds["표준화"] = sig[t] + statistics.fmean(vol[i] - sig[i] for i in near)

            if world == "무작위":
                ranges["정답"] = (-oracle, oracle)
                preds["정답"] = mean_vol
            elif world == "군집":
                total_var = garch_total(info[t], h, s)
                band = Z80 * math.sqrt(total_var) * DAILY
                ranges["정답"] = (-band, band)
                raw = 0.5 * math.log(total_var / h) + math.log(DAILY)
                bias = statistics.fmean(
                    vol[i] - (0.5 * math.log(garch_total(info[i], h, s) / h) + math.log(DAILY))
                    for i in range(t - h - 60, t - h + 1))
                preds["정답"] = raw + bias
            else:
                lo, hi, mv = table[info[t]]
                ranges["정답"] = (lo, hi)
                preds["정답"] = mv

            ranges["과거"] = (quantile(past, 0.1), quantile(past, 0.9))
            preds["과거"] = mean_vol
            b1 = Z80 * s_now * root
            ranges["최근"] = (-b1, b1)
            preds["최근"] = sig[t]
            b2 = Z80 * math.exp((sig[t] + mean_sig) / 2) * root
            ranges["섞음"] = (-b2, b2)
            preds["섞음"] = (sig[t] + mean_vol) / 2

            loss_r = {k: pinball(y, *v) for k, v in ranges.items()}
            loss_v = {k: abs(yv - v) for k, v in preds.items()}
            for m in METHODS:
                for b in BASES:
                    day[("range", m, b)].append(loss_r[m] - loss_r[b])
                    day[("vol", m, b)].append(loss_v[m] - loss_v[b])
            for k, (lo, hi) in ranges.items():
                hits[k] += lo <= y <= hi
            total += 1
        for key, vals in day.items():
            diffs[key].append(statistics.fmean(vals))
    out: dict = {"dates": len(dates)}
    for key, vals in diffs.items():
        out[key] = passes(vals)
    for tg in ("range", "vol"):
        for m in METHODS:
            out[(tg, m, "모두")] = all(out[(tg, m, b)] for b in BASES)
    for k, v in hits.items():
        out[("cov", k)] = v / total
    return out


def run_cell(args):
    world, idx = args
    cfg = SIZES[idx]
    seed = 20261106 + idx * 1000 + WORLDS.index(world)
    rng = random.Random(seed)
    oracle = oracle_band(cfg["h"], cfg["s"], rng) if world == "무작위" else None
    table = regime_table(cfg["h"], cfg["s"], rng) if world == "국면" else None
    reps = [one_rep(world, cfg, oracle, table, rng) for _ in range(cfg["reps"])]
    out = {"world": world, "size": cfg["name"], "reps": cfg["reps"], "seed": seed,
           "dates": reps[0]["dates"], "pairs": reps[0]["dates"] * cfg["s"]}
    for tg in ("range", "vol"):
        for m in METHODS:
            for b in BASES + ["모두"]:
                out[f"pass_{tg}_{m}_{b}"] = round(sum(r[(tg, m, b)] for r in reps) / len(reps), 3)
    for k in METHODS + BASES:
        vals = sorted(r[("cov", k)] for r in reps)
        out[f"cov_{k}"] = round(statistics.fmean(vals), 3)
        out[f"cov_{k}_p2_5"] = round(quantile(vals, 0.025), 3)
        out[f"cov_{k}_p97_5"] = round(quantile(vals, 0.975), 3)
        out[f"cov_{k}_in_75_85"] = round(sum(0.75 <= v <= 0.85 for v in vals) / len(vals), 3)
        out[f"cov_{k}_in_70_90"] = round(sum(0.70 <= v <= 0.90 for v in vals) / len(vals), 3)
    return out


def episode_count():
    """이웃 30개가 실제로 몇 번의 서로 겹치지 않는 때인지 잰다. 사용: --episodes"""
    print("| 데이터 | 세계 | 겹치지 않는 때의 평균 수(이웃 30개 가운데) |")
    print("|---|---|---|")
    for idx in (0, 4):
        cfg = SIZES[idx]
        for world in WORLDS:
            rng = random.Random(20261106 + idx)
            counts = []
            for _ in range(20):
                r = [x * DAILY for x in series(world, cfg["n"], rng)[0]]
                sig, ret, _, _ = prepare(r, cfg["h"])
                for t in range(WARMUP, cfg["n"] - cfg["h"], cfg["h"]):
                    last = None
                    c = 0
                    for i in sorted(neighbors(sig, ret, t, cfg["h"], K_NAIVE)):
                        if last is None or i - last >= cfg["h"]:
                            c += 1
                            last = i
                    counts.append(c)
            print(f"| {cfg['name']} | {world} | {statistics.fmean(counts):.1f} |")


def main():
    if "--episodes" in sys.argv:
        episode_count()
        return
    cells = [(w, i) for i in range(len(SIZES)) for w in WORLDS]
    with ProcessPoolExecutor() as pool:
        rows = list(pool.map(run_cell, cells))
    (HERE / "power_result.json").write_text(
        json.dumps({"note": "합성 세계에서 잰 값입니다. 실제 시장의 값이 아닙니다.",
                    "rows": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("| 데이터 | 세계 | 시험 날짜 | 정답 적중 95% 범위 | 75 ~ 85% | 70 ~ 90% | 이웃 적중 | 범위 통과: 이웃, 표준화, 정답 | 변동성 통과: 이웃, 표준화, 정답 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['size']} | {r['world']} | {r['dates']} | {r['cov_정답_p2_5']:.0%} ~ {r['cov_정답_p97_5']:.0%} | "
              f"{r['cov_정답_in_75_85']:.0%} | {r['cov_정답_in_70_90']:.0%} | {r['cov_이웃']:.0%} | "
              f"{r['pass_range_이웃_모두']:.0%}, {r['pass_range_표준화_모두']:.0%}, {r['pass_range_정답_모두']:.0%} | "
              f"{r['pass_vol_이웃_모두']:.0%}, {r['pass_vol_표준화_모두']:.0%}, {r['pass_vol_정답_모두']:.0%} |")


if __name__ == "__main__":
    main()
