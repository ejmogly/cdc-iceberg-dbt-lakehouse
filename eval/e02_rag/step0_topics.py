#!/usr/bin/env python3
"""E02 0단계 보충 조사: 종목 표시 없이 받은 기사에서 주제어로 찾으면 근거가 늘어나는지 잰다.

종목 표시만으로는 기사가 거의 없는 ETF의 큰 움직임 날과 평범한 날을 뽑아, 그 창의 전체 기사를 받고
자산군 주제어가 제목이나 요약에 나온 기사 수를 센다. 평범한 날과 비교해야 주제어가 그날에 특별한지 알 수 있다.
사용: python3 step0_topics.py   (step0_survey.py를 먼저 돌려 시세가 있어야 함. 기사 내용은 출력하지 않음)
"""
import json
import random
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from step0_survey import HOST, WAIT, big_move_days, trading_days, windows  # noqa: E402
from step0_util import ROOT, get_json, load_key  # noqa: E402

HERE = Path(__file__).parent
RAW = ROOT / "data" / "raw" / "e02_step0"
SEED = 20261106
BIG_PER_SYMBOL, CALM_PER_SYMBOL, MAX_PAGES = 3, 1, 3
TOPICS = {
    "IWM": r"small[- ]cap|russell 2000",
    "SHY": r"treasur|federal reserve|\bfed\b|fomc|rate cut|rate hike",
    "TLT": r"treasur|bond yield|federal reserve|\bfed\b|fomc",
    "HYG": r"high[- ]yield|junk bond|credit spread|corporate bond",
    "GLD": r"\bgold\b|bullion",
    "USO": r"crude|\boil\b|opec|brent|\bwti\b",
    "UUP": r"\bdollar\b|greenback|\bdxy\b",
    "EFA": r"europe|japan|nikkei|\becb\b|developed market",
    "EEM": r"emerging market|china|india|brazil",
}


def fetch_window(key, a, b):
    name = RAW / "topics" / f"{a.astimezone(timezone.utc):%Y%m%dT%H%M}_{b.astimezone(timezone.utc):%Y%m%dT%H%M}.json"
    if name.exists():
        return json.loads(name.read_text(encoding="utf-8"))
    name.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    url = (f"{HOST}/v2/reference/news?published_utc.gte={a.astimezone(timezone.utc).strftime(fmt)}"
           f"&published_utc.lte={b.astimezone(timezone.utc).strftime(fmt)}&order=asc&sort=published_utc&limit=1000")
    items, pages = [], 0
    while url and pages < MAX_PAGES:
        code, data = get_json(url, [f"Authorization: Bearer {key}"])
        time.sleep(WAIT)
        if code == 429:
            time.sleep(60)
            continue
        if code != 200 or data is None:
            raise SystemExit(f"요청 실패: HTTP {code}")
        items += [{"id": x["id"], "published_utc": x["published_utc"], "publisher": (x.get("publisher") or {}).get("name", ""),
                   "tickers": x.get("tickers", []), "title": x.get("title", ""), "description": x.get("description", "")}
                  for x in data.get("results", [])]
        url = data.get("next_url")
        pages += 1
    out = {"capped": bool(url), "items": items}
    name.write_text(json.dumps(out), encoding="utf-8")
    return out


def main():
    key = load_key("MASSIVE_API_KEY")
    rng = random.Random(SEED)
    now = datetime.now(timezone.utc)
    rows = []
    for sym, pattern in TOPICS.items():
        prices = json.loads((RAW / f"{sym}.prices.json").read_text(encoding="utf-8"))
        days = trading_days(prices)
        win = windows(days, now)
        big = big_move_days(prices, days)
        calm = [d for d in days[61:-1] if d not in set(big)]
        chosen = [("큰 움직임", d) for d in rng.sample(big, min(BIG_PER_SYMBOL, len(big)))]
        chosen += [("평범한 날", d) for d in rng.sample(calm, CALM_PER_SYMBOL)]
        rx = re.compile(pattern, re.I)
        for kind, d in chosen:
            got = fetch_window(key, *win[d])
            hits = [i for i in got["items"] if rx.search(f"{i['title']} {i['description']}")]
            rows.append({"symbol": sym, "kind": kind, "articles": len(got["items"]), "capped": got["capped"],
                         "topic_hits": len(hits), "topic_in_title": sum(bool(rx.search(i["title"])) for i in hits)})
            print(f"{sym} {kind}: 전체 {len(got['items'])}, 주제어 {len(hits)}", flush=True)

    def stat(kind):
        r = [x for x in rows if x["kind"] == kind]
        return {"windows": len(r), "articles_median": statistics.median(x["articles"] for x in r),
                "topic_hits_median": statistics.median(x["topic_hits"] for x in r),
                "topic_in_title_median": statistics.median(x["topic_in_title"] for x in r),
                "with_title_hit_pct": round(sum(x["topic_in_title"] > 0 for x in r) / len(r) * 100, 1),
                "capped": sum(x["capped"] for x in r)}
    out = {"note": "기사 수 집계만 둡니다. 날짜는 시세에서 뽑았고 저장하지 않습니다.",
           "big": stat("큰 움직임"), "calm": stat("평범한 날"),
           "rows": [{k: v for k, v in r.items() if k != "date"} for r in rows]}
    (HERE / "step0_topics_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"큰 움직임": out["big"], "평범한 날": out["calm"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
