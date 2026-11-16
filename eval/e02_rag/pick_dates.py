#!/usr/bin/env python3
"""E02 평가셋 날짜 40개를 정답 고정 전에 미리 고른다. 사용: .venv/bin/python eval/e02_rag/pick_dates.py [--no-news]

기준은 docs/ai/experiments/E02-rag.md 3절(범위, 평가셋)과 docs/ai/F-screen.md 4절 평가셋의 「E02」 줄이다.

- 종목: E02 1판 범위인 미국 17종목(loaders/instruments.csv에서 market이 US). 국내 2종목은 뺀다.
- 시세: data/raw/daily_prices/us_{SYM}_20241113_20261112_adjtrue.json(수정한 가격, git 제외).
- 큰 움직임: step0_survey.py의 big_move_days를 그대로 쓴다. 단순 수익률(종가/전날 종가 - 1)의 절댓값이
  그날을 뺀 앞 60거래일 수익률의 표본 표준편차(n - 1로 나눔)의 2배를 넘은 날이다.
- 평범한 날(문서에 정의가 없어 여기서 정함): 같은 표준편차로 하루 등락의 절댓값이 0.5배 이하이고,
  그 종목의 앞뒤 거래일도 큰 움직임이 아닌 날. 검색 창이 앞뒤 거래일에 걸치므로 이웃한 큰 움직임을 피한다.
- 근거를 뺀 날: 큰 움직임 날 가운데 고르고, 큰 움직임 묶음 24개와 겹치지 않게 한다.
- 고른 40개는 서로 검색 창이 겹치지 않는다(거래일로 2일 이상 떨어짐). 같은 기사가 두 문항에 들지 않게 하려는 것이다.

고른 결과는 eval_dates.json에 둔다. 날짜, 종목, 묶음, 나눔, 자산군만 두고 가격과 등락률은 넣지 않는다(재배포 금지).
기사 수는 data/raw/news_massive/ALL_20241113_20261112.json에서 고른 날의 검색 창에 든 건수만 세어 출력한다.
고르는 데에는 기사를 쓰지 않는다(기사가 있는 날만 골라 평가가 쉬워지지 않게). 기사 내용은 출력하지 않는다.
시드를 고정했고 만든 시각을 파일에 넣지 않으므로, 다시 돌리면 같은 파일이 나온다.
"""
import bisect
import csv
import json
import random
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # 가져오는 스크립트의 캐시 파일(__pycache__)을 남기지 않음
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from step0_survey import big_move_days, trading_days, windows  # noqa: E402  같은 식을 쓰려고 그대로 가져옴

ROOT = HERE.parents[1]
TAG = "20241113_20261112"
PRICES = ROOT / "data" / "raw" / "daily_prices"
NEWS = ROOT / "data" / "raw" / "news_massive" / f"ALL_{TAG}.json"
OUT = HERE / "eval_dates.json"
NEWS_START = datetime(2024, 11, 13, tzinfo=timezone.utc)  # 받은 전체 기사의 기간(UTC 날짜 2024-11-13 ~ 2026-11-12)
NEWS_END = datetime(2026, 11, 13, tzinfo=timezone.utc)

SEED = 20261113
LOOKBACK = 60          # 표준편차를 재는 앞 거래일 수(step0_survey.py와 같음)
FIRST = LOOKBACK + 1   # 앞 60개 수익률이 다 있는 첫 거래일 번호(step0_survey.py의 range(61, ...)와 같음)
CALM_RATIO = 0.5       # 평범한 날: 하루 등락의 절댓값이 표준편차의 이 배 이하
GAP = 2                # 고른 날끼리 떨어져야 하는 거래일 수. 2 이상이면 검색 창이 겹치지 않음

# 묶음 이름과 수. 묶음마다 개발 수는 전체 비율(24 / 40 = 0.6)에 맞춤: 14.4, 4.8, 4.8을 합이 24가 되게 반올림
# 순서는 E02 문서 3절 「평가셋」 표와 같고, 이 순서로 고른다(앞 묶음에서 고른 날과 그 앞뒤 날은 뒤 묶음 후보에서 빠짐)
SETS = {
    "big_move": {"name": "큰 움직임", "n": 24, "dev": 14},
    "calm": {"name": "평범한 날", "n": 8, "dev": 5},
    "no_evidence": {"name": "근거를 뺀 날", "n": 8, "dev": 5},
}
DEV_TOTAL, CONFIRM_TOTAL = 24, 16

# 큰 움직임 24개를 자산군(instruments.csv의 group)에 나눈 몫.
# 17종목이 모두 한 번 이상 나오게 하면 개별 기업은 5가 되고, 남은 19를 ETF 자산군 여섯 곳에 3, 3, 3, 3, 3, 4로 나눈다.
# 4는 원자재에 준다. 금과 원유는 다른 시장이라 종목마다 2일씩 둔다.
BIG_QUOTA = {"미국 주식": 3, "금리와 신용": 3, "원자재": 4, "통화": 3, "미국 밖": 3, "가상자산": 3, "미국 개별 기업": 5}
# 근거를 뺀 날과 평범한 날은 8개씩. 자산군마다 1개, 남은 1개는 종목이 가장 많은 개별 기업에 준다.
SMALL_QUOTA = {"미국 주식": 1, "금리와 신용": 1, "원자재": 1, "통화": 1, "미국 밖": 1, "가상자산": 1, "미국 개별 기업": 2}


def us_instruments():
    """[(종목, 자산군)]. 자산군은 instruments.csv의 group 칸."""
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8") as f:
        return [(r["symbol"], r["group"]) for r in csv.DictReader(f) if r["market"] == "US"]


def load_symbol(sym):
    """거래일 목록, 큰 움직임 날 번호, 평범한 날 후보 번호를 낸다. 가격과 수익률은 밖으로 내지 않는다."""
    path = PRICES / f"us_{sym}_{TAG}_adjtrue.json"
    prices = json.loads(path.read_text(encoding="utf-8"))["results"]
    days = trading_days(prices)
    rets = [None] + [prices[i]["c"] / prices[i - 1]["c"] - 1 for i in range(1, len(prices))]
    big, small = set(), set()
    for i in range(FIRST, len(prices)):
        sd = statistics.stdev(rets[i - LOOKBACK:i])  # 그날은 넣지 않음
        if abs(rets[i]) > 2 * sd:
            big.add(i)
        elif abs(rets[i]) <= CALM_RATIO * sd:
            small.add(i)
    # 같은 식인지 확인: step0_survey.py의 함수가 낸 날과 하나도 다르지 않아야 한다
    same = {days.index(d) for d in big_move_days(prices, days)}
    if same != big:
        raise SystemExit(f"{sym}: 큰 움직임 날이 step0_survey.py와 다릅니다")
    # 평범한 날은 앞뒤 거래일도 큰 움직임이 아니어야 한다. 앞날의 판정이 없는 첫날(FIRST)은 뺀다
    calm = {i for i in small if i - 1 >= FIRST and i + 1 < len(prices) and i - 1 not in big and i + 1 not in big}
    return days, big, calm


def make_bins(lo, hi, n):
    """[lo, hi)의 거래일 번호를 n칸으로 똑같이 나눈다. 칸의 마지막 거래일은 후보에서 빼서
    이웃한 두 칸에서 뽑힌 날이 늘 2거래일 이상 떨어지게 한다."""
    edges = [lo + (hi - lo) * k // n for k in range(n + 1)]
    return [range(edges[k], edges[k + 1] - 1) for k in range(n)]


def symbol_slots(quota, order, cursor):
    """자산군 몫을 종목 칸으로 바꾼다. 자산군마다 시드로 한 번 섞은 종목 순서(order)를 묶음끼리 이어서 돌아가며 쓴다.
    그래서 평범한 날과 근거를 뺀 날은 큰 움직임에서 덜 나온 종목부터, 두 묶음이 서로 다른 종목부터 쓴다."""
    slots = []
    for group, count in quota.items():
        seq = order[group]
        slots += [seq[(cursor[group] + k) % len(seq)] for k in range(count)]
        cursor[group] += count
    return slots


def match(slots, bins, cands, rng):
    """칸 하나에 종목 칸 하나를 짝짓는다(이분 매칭, 증가 경로). 그 칸 안에 그 종목의 후보가 있어야 짝이 된다.
    순서를 시드로 섞어 같은 시드면 같은 짝이 나온다. 짝이 다 맞지 않으면 멈춘다."""
    adj = []
    for sym in slots:
        ok = [b for b, r in enumerate(bins) if any(i in cands[sym] for i in r)]
        rng.shuffle(ok)
        adj.append(ok)
    owner = [None] * len(bins)

    def augment(k, seen):
        for b in adj[k]:
            if b in seen:
                continue
            seen.add(b)
            if owner[b] is None or augment(owner[b], seen):
                owner[b] = k
                return True
        return False

    order = list(range(len(slots)))
    rng.shuffle(order)
    for k in order:
        if not augment(k, set()):
            raise SystemExit(f"{slots[k]}: 기간 칸에 짝을 지을 후보가 모자랍니다. 몫이나 칸 수를 다시 봐야 합니다")
    return owner


def pick_set(key, slots, cands, lo, hi, taken):
    """묶음 하나를 고른다. 기간을 종목 칸 수만큼 똑같이 나눠 칸마다 하나씩 고르므로 기간이 고르게 퍼진다."""
    rng = random.Random(f"{SEED}:{key}")
    bins = make_bins(lo, hi, len(slots))
    blocked = {t + d for t in taken for d in range(-(GAP - 1), GAP)}
    free = {sym: cands[sym] - blocked for sym in cands}
    owner = match(slots, bins, free, rng)
    out = []
    for b, k in enumerate(owner):
        sym = slots[k]
        out.append((sym, rng.choice([i for i in bins[b] if i in free[sym]])))
    return out


def split(rows, dev_n, rng):
    """묶음 안에서 개발과 확인을 나눈다. 자산군마다 개발 몫을 비율대로 주고(최대 나머지 방식, 나머지가 같으면 시드 순서),
    자산군 안에서 시드로 고른다. 그래서 2일 이상인 자산군은 개발과 확인에 모두 들어간다."""
    groups = {}
    for r in rows:
        groups.setdefault(r["asset_group"], []).append(r)
    total = len(rows)
    exact = {g: Fraction(len(v) * dev_n, total) for g, v in groups.items()}
    share = {g: int(x) for g, x in exact.items()}
    tie = {g: rng.random() for g in groups}
    order = sorted(groups, key=lambda g: (-(exact[g] - share[g]), tie[g]))
    for g in order[:dev_n - sum(share.values())]:
        share[g] += 1
    for g, v in groups.items():
        dev = {id(r) for r in rng.sample(v, share[g])}
        for r in v:
            r["split"] = "dev" if id(r) in dev else "confirm"


def count_news(rows, days):
    """고른 날마다 검색 창(step0_survey.windows와 같음)에 든 기사 수를 센다. 창 전체 기사와 그 종목 표시 기사."""
    pages = json.loads(NEWS.read_text(encoding="utf-8"))["pages"]
    stamps, tagged = [], {}
    for page in pages:
        for it in page["results"]:
            s = datetime.fromisoformat(it["published_utc"].replace("Z", "+00:00")).timestamp()
            stamps.append(s)
            for t in it.get("tickers") or []:
                tagged.setdefault(t, []).append(s)
    stamps.sort()
    for v in tagged.values():
        v.sort()
    win = windows(days, NEWS_END)  # 마지막 거래일에만 NEWS_END를 쓰는데, 마지막 거래일은 고르지 않는다
    for r in rows:
        a, b = win[r["_day"]]
        if a < NEWS_START or b > NEWS_END:
            raise SystemExit(f"{r['id']}: 검색 창이 받은 기사 기간 밖입니다")
        lo, hi = a.timestamp(), b.timestamp()
        r["_all"] = bisect.bisect_left(stamps, hi) - bisect.bisect_left(stamps, lo)
        mine = tagged.get(r["symbol"], [])
        r["_tag"] = bisect.bisect_left(mine, hi) - bisect.bisect_left(mine, lo)
        r["_hours"] = round((b - a).total_seconds() / 3600, 1)
    return len(stamps)


def table(title, rows, key, cols):
    print(f"\n{title}")
    print("| 묶음 | " + " | ".join(cols) + " | 합 |")
    print("|---|" + "---:|" * (len(cols) + 1))
    for s in SETS:
        c = Counter(key(r) for r in rows if r["set"] == s)
        print(f"| {SETS[s]['name']} | " + " | ".join(str(c.get(x, 0)) for x in cols) + f" | {sum(c.values())} |")


def spread(vals):
    return f"{min(vals)} / {statistics.median(vals)} / {max(vals)}" if vals else "없음"


def main():
    inst = us_instruments()
    members = {}
    for sym, group in inst:
        members.setdefault(group, []).append(sym)
    assert set(members) == set(BIG_QUOTA) == set(SMALL_QUOTA), "자산군 이름이 instruments.csv와 다릅니다"
    assert sum(BIG_QUOTA.values()) == SETS["big_move"]["n"]
    assert sum(SMALL_QUOTA.values()) == SETS["calm"]["n"] == SETS["no_evidence"]["n"]
    assert sum(s["dev"] for s in SETS.values()) == DEV_TOTAL
    assert sum(s["n"] - s["dev"] for s in SETS.values()) == CONFIRM_TOTAL

    days, big, calm = None, {}, {}
    for sym, _ in inst:
        d, big[sym], calm[sym] = load_symbol(sym)
        if days is None:
            days = d
        elif d != days:
            raise SystemExit(f"{sym}: 거래일이 다른 종목과 다릅니다")
    # 고를 수 있는 기간: 앞 60개 수익률이 있는 첫날부터, 다음 거래일 장 시작이 데이터 안에 있는 마지막 전날까지
    lo, hi = FIRST, len(days) - 1
    big = {s: {i for i in v if lo <= i < hi} for s, v in big.items()}
    calm = {s: {i for i in v if lo <= i < hi} for s, v in calm.items()}

    # 자산군마다 종목 순서를 시드로 한 번 섞고, 세 묶음이 이 순서를 이어서 쓴다
    rng = random.Random(f"{SEED}:order")
    order, cursor = {}, {}
    for group in BIG_QUOTA:
        order[group] = list(members[group])
        rng.shuffle(order[group])
        cursor[group] = 0
    taken, picked = set(), []
    plan = [("big_move", BIG_QUOTA, big), ("calm", SMALL_QUOTA, calm), ("no_evidence", SMALL_QUOTA, big)]
    assert [k for k, _, _ in plan] == list(SETS)
    for key, quota, cands in plan:
        got = pick_set(key, symbol_slots(quota, order, cursor), cands, lo, hi, taken)
        taken |= {i for _, i in got}
        picked.append((key, sorted(got, key=lambda x: (x[1], x[0]))))

    group_of = dict(inst)
    rows = []
    for key, got in picked:
        part = [{"_day": days[i], "date": days[i].isoformat(), "symbol": sym, "set": key,
                 "asset_group": group_of[sym]} for sym, i in got]
        split(part, SETS[key]["dev"], random.Random(f"{SEED}:split:{key}"))
        rows += part
    for n, r in enumerate(rows, 1):
        r["id"] = f"E02-{n:02d}"

    # 검사: 서로 검색 창이 겹치지 않고, 근거를 뺀 날은 큰 움직임 날이며 큰 움직임 묶음과 겹치지 않음
    idx = sorted(days.index(r["_day"]) for r in rows)
    assert all(b - a >= GAP for a, b in zip(idx, idx[1:])), "고른 날끼리 검색 창이 겹칩니다"
    for r in rows:
        i = days.index(r["_day"])
        assert i in (calm if r["set"] == "calm" else big)[r["symbol"]]
    assert sum(r["split"] == "dev" for r in rows) == DEV_TOTAL

    keep = ("id", "date", "symbol", "set", "split", "asset_group")
    doc = {
        "note": "E02 평가셋 날짜(정답 고정 전에 고름). 가격과 등락률은 넣지 않는다(재배포 금지). "
                "만든 곳: eval/e02_rag/pick_dates.py, 기준: docs/ai/experiments/E02-rag.md 3절",
        "seed": SEED,
        "prices": f"data/raw/daily_prices/us_{{SYM}}_{TAG}_adjtrue.json (수정한 가격)",
        "range": f"{days[lo].isoformat()} ~ {days[hi - 1].isoformat()}",
        "sets": {k: v["name"] for k, v in SETS.items()},
        "splits": {"dev": "개발", "confirm": "확인(마지막 판에 한 번만)"},
        "definitions": {
            "big_move": "단순 수익률의 절댓값이 그날을 뺀 앞 60거래일 수익률 표본 표준편차의 2배 초과(step0_survey.py와 같은 식)",
            "calm": "같은 표준편차의 0.5배 이하이고, 그 종목의 앞뒤 거래일도 큰 움직임이 아닌 날",
            "no_evidence": "큰 움직임 날 가운데 큰 움직임 묶음과 겹치지 않는 날. 평가 때 근거를 일부러 뺌",
            "asset_group": "loaders/instruments.csv의 group",
            "spacing": "고른 40개는 서로 2거래일 이상 떨어져 검색 창이 겹치지 않음",
        },
        "items": [{k: r[k] for k in keep} for r in rows],
    }
    # 문항은 한 줄에 하나(eval/e04_agent/questions.json과 같은 모양)
    head = json.dumps({k: v for k, v in doc.items() if k != "items"}, ensure_ascii=False, indent=1)
    lines = [json.dumps(x, ensure_ascii=False) for x in doc["items"]]
    OUT.write_text(head[:-2] + ',\n "items": [\n  ' + ",\n  ".join(lines) + "\n ]\n}\n", encoding="utf-8")
    assert json.loads(OUT.read_text(encoding="utf-8")) == doc

    # 출력: 후보 수, 고른 것, 분포. 가격과 수익률은 내지 않는다
    print(f"고를 수 있는 기간: {doc['range']} ({hi - lo}거래일), 시드 {SEED}")
    print("\n| 종목 | 자산군 | 큰 움직임 후보 | 평범한 날 후보 |")
    print("|---|---|---:|---:|")
    for sym, group in inst:
        print(f"| {sym} | {group} | {len(big[sym])} | {len(calm[sym])} |")

    news_total = None
    if "--no-news" not in sys.argv and NEWS.exists():
        news_total = count_news(rows, days)

    print("\n| 번호 | 날짜 | 종목 | 묶음 | 나눔 | 자산군 | 창 전체 기사 | 종목 표시 기사 |")
    print("|---|---|---|---|---|---|---:|---:|")
    for r in rows:
        extra = f"{r['_all']} | {r['_tag']}" if news_total else " | "
        print(f"| {r['id']} | {r['date']} | {r['symbol']} | {SETS[r['set']]['name']} | "
              f"{'개발' if r['split'] == 'dev' else '확인'} | {r['asset_group']} | {extra} |")

    groups = list(BIG_QUOTA)
    table("자산군 분포", rows, lambda r: r["asset_group"], groups)
    months = sorted({d.isoformat()[:7] for d in days[lo:hi]})
    quarters = sorted({f"{m[:4]}Q{(int(m[5:]) - 1) // 3 + 1}" for m in months})
    table("분기 분포", rows, lambda r: f"{r['date'][:4]}Q{(int(r['date'][5:7]) - 1) // 3 + 1}", quarters)
    print("\n월별 분포(" + " / ".join(v["name"] for v in SETS.values()) + ")")
    for m in months:
        c = [sum(1 for r in rows if r["set"] == s and r["date"][:7] == m) for s in SETS]
        print(f"  {m}: {c[0]} / {c[1]} / {c[2]}")

    print("\n나눔")
    print("| 묶음 | 개발 | 확인 | 개발의 자산군 | 확인의 자산군 |")
    print("|---|---:|---:|---|---|")
    for s in SETS:
        part = [r for r in rows if r["set"] == s]
        dev = [r for r in part if r["split"] == "dev"]
        conf = [r for r in part if r["split"] == "confirm"]
        fmt = lambda xs: ", ".join(f"{g} {n}" for g, n in Counter(r["asset_group"] for r in xs).items())  # noqa: E731
        print(f"| {SETS[s]['name']} | {len(dev)} | {len(conf)} | {fmt(dev)} | {fmt(conf)} |")
    print(f"| 합 | {sum(r['split'] == 'dev' for r in rows)} | {sum(r['split'] == 'confirm' for r in rows)} | | |")
    for sp in ("dev", "confirm"):
        c = Counter(f"{r['date'][:4]}Q{(int(r['date'][5:7]) - 1) // 3 + 1}" for r in rows if r["split"] == sp)
        print(f"  {'개발' if sp == 'dev' else '확인'} 분기: " + ", ".join(f"{q} {c.get(q, 0)}" for q in quarters))

    if news_total:
        print(f"\n검색 창 안 기사 수(받은 기사 {news_total:,}건, 최소 / 중앙값 / 최대)")
        print("| 묶음 | 창 전체 기사 | 0건인 날 | 개별 기업의 종목 표시 기사 | ETF의 종목 표시 기사 0건인 날 |")
        print("|---|---|---:|---|---:|")
        for s in SETS:
            part = [r for r in rows if r["set"] == s]
            stock = [r["_tag"] for r in part if r["asset_group"] == "미국 개별 기업"]
            etf0 = sum(1 for r in part if r["asset_group"] != "미국 개별 기업" and r["_tag"] == 0)
            etf_n = sum(1 for r in part if r["asset_group"] != "미국 개별 기업")
            print(f"| {SETS[s]['name']} | {spread([r['_all'] for r in part])} | {sum(r['_all'] == 0 for r in part)} | "
                  f"{spread(stock)} | {etf0} / {etf_n} |")
        hours = [r["_hours"] for r in rows]
        print(f"  검색 창 길이(시간): {spread(hours)}")
    elif "--no-news" not in sys.argv:
        print(f"\n기사 원본이 없어 기사 수는 세지 않았습니다: {NEWS.relative_to(ROOT)}")
    print(f"\n{OUT.relative_to(ROOT)}에 {len(rows)}개를 썼습니다.")


if __name__ == "__main__":
    main()
