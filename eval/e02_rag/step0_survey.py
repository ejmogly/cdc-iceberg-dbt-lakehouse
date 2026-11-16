#!/usr/bin/env python3
"""E02 0단계 조사: 하루에 기사가 몇 건이나 있는지 잰다. 사용: python3 step0_survey.py [--raw 폴더] [--no-fetch] [--symbols A,B --out 파일이름]

Massive 무료 요금제(분당 5회)로 ETF 12개의 일별 시세와 종목 뉴스를 받아 data/raw/(git 제외)에 두고,
기사 수만 step0_result.json에 남긴다. 시세, 수익률, 기사 제목은 출력하지 않는다.
이미 받은 파일은 다시 받지 않으므로 중간에 멈춰도 이어서 돌릴 수 있다.
"""
import json
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from step0_util import ROOT, get_json, load_key  # noqa: E402

HERE = Path(__file__).parent
HOST = "https://api.massive.com"
SYMBOLS = ["SPY", "QQQ", "IWM", "SHY", "TLT", "HYG", "GLD", "USO", "UUP", "EFA", "EEM", "IBIT"]
ET = ZoneInfo("America/New_York")
WAIT = 13  # 분당 5회 제한
MAX_PAGES = 40


def fetch(url, key):
    while True:
        code, data = get_json(url, [f"Authorization: Bearer {key}"])
        time.sleep(WAIT)
        if code == 429:
            print("  분당 제한에 걸려 1분 기다립니다", flush=True)
            time.sleep(60)
            continue
        if code != 200 or data is None:
            raise SystemExit(f"요청 실패: HTTP {code}. {(data or {}).get('error') or (data or {}).get('message') or ''}")
        return data


def download(raw, key, start, end):
    raw.mkdir(parents=True, exist_ok=True)
    for sym in SYMBOLS:
        price_file = raw / f"{sym}.prices.json"
        if not price_file.exists():
            data = fetch(f"{HOST}/v2/aggs/ticker/{sym}/range/1/day/{start}/{end}?adjusted=true&sort=asc&limit=50000", key)
            price_file.write_text(json.dumps(data.get("results", [])), encoding="utf-8")
            print(f"{sym} 시세 받음", flush=True)
        news_file = raw / f"{sym}.news.json"
        if not news_file.exists():
            url = (f"{HOST}/v2/reference/news?ticker={sym}&published_utc.gte={start}&published_utc.lte={end}"
                   "&order=asc&sort=published_utc&limit=1000")
            items, pages = [], 0
            while url and pages < MAX_PAGES:
                data = fetch(url, key)
                items += data.get("results", [])
                url = data.get("next_url")
                pages += 1
            news_file.write_text(json.dumps({"capped": bool(url), "pages": pages, "items": items}), encoding="utf-8")
            print(f"{sym} 뉴스 {pages}쪽 받음", flush=True)


def trading_days(prices):
    return [datetime.fromtimestamp(r["t"] / 1000, timezone.utc).astimezone(ET).date() for r in prices]


def windows(days, now):
    """거래일 t의 검색 창: 전날 장 마감부터 다음 날 장 시작 전까지(미국 동부)."""
    out = {}
    for i in range(1, len(days)):
        a = datetime.combine(days[i - 1], datetime.min.time(), ET) + timedelta(hours=16)
        b = (datetime.combine(days[i + 1], datetime.min.time(), ET) + timedelta(hours=9, minutes=30)
             if i + 1 < len(days) else now)
        out[days[i]] = (a, b)
    return out


def big_move_days(prices, days):
    """하루 등락의 절댓값이 그 전 60일 표준편차의 2배를 넘은 날."""
    rets = [None] + [prices[i]["c"] / prices[i - 1]["c"] - 1 for i in range(1, len(prices))]
    out = []
    for i in range(61, len(prices)):
        if abs(rets[i]) > 2 * statistics.stdev(rets[i - 60:i]):
            out.append(days[i])
    return out


def pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def summarize(sym, raw, now):
    prices = json.loads((raw / f"{sym}.prices.json").read_text(encoding="utf-8"))
    news = json.loads((raw / f"{sym}.news.json").read_text(encoding="utf-8"))
    seen, items = set(), []
    for it in news["items"]:
        if it["id"] not in seen:
            seen.add(it["id"])
            items.append(it)
    days = trading_days(prices)
    win = windows(days, now)
    stamps = sorted(datetime.fromisoformat(it["published_utc"].replace("Z", "+00:00")) for it in items)
    counts = {d: 0 for d in win}
    # 창이 겹치므로 기사 하나가 이틀에 세어질 수 있다. 질문 하나가 받는 후보 수를 재는 것이다.
    for d, (a, b) in win.items():
        counts[d] = sum(1 for s in stamps if a <= s < b)
    big = big_move_days(prices, days)
    all_counts = list(counts.values())
    big_counts = [counts[d] for d in big]
    n_tickers = [len(it.get("tickers", [])) for it in items]
    publishers = Counter((it.get("publisher") or {}).get("name", "") for it in items)
    return {
        "symbol": sym,
        "first_day": days[0].isoformat() if days else None,
        "last_day": days[-1].isoformat() if days else None,
        "trading_days": len(days),
        "articles": len(items),
        "capped": news["capped"],
        "first_article": stamps[0].date().isoformat() if stamps else None,
        "days_with_article_pct": pct(sum(c > 0 for c in all_counts), len(all_counts)),
        "per_day_median": statistics.median(all_counts) if all_counts else None,
        "per_day_p90": sorted(all_counts)[int(len(all_counts) * 0.9)] if all_counts else None,
        "big_days": len(big),
        "big_with_article_pct": pct(sum(c > 0 for c in big_counts), len(big_counts)),
        "big_median": statistics.median(big_counts) if big_counts else None,
        "big_over5_pct": pct(sum(c > 5 for c in big_counts), len(big_counts)),
        "tickers_per_article_median": statistics.median(n_tickers) if n_tickers else None,
        "focused_pct": pct(sum(n <= 3 for n in n_tickers), len(n_tickers)),
        "with_insight_pct": pct(sum(any(x.get("ticker") == sym for x in it.get("insights") or []) for it in items), len(items)),
        "top_publisher_pct": pct(publishers.most_common(1)[0][1], len(items)) if items else None,
        "_all": all_counts,
        "_big": big_counts,
    }


def main():
    global SYMBOLS
    raw = ROOT / "data" / "raw" / "e02_step0"
    out_name = "step0_result.json"
    if "--symbols" in sys.argv:
        SYMBOLS = sys.argv[sys.argv.index("--symbols") + 1].split(",")
        out_name = sys.argv[sys.argv.index("--out") + 1]
    if "--raw" in sys.argv:
        raw = Path(sys.argv[sys.argv.index("--raw") + 1])
    today = date.today()
    start, end = (today - timedelta(days=730)).isoformat(), today.isoformat()
    if "--no-fetch" not in sys.argv:
        key = load_key("MASSIVE_API_KEY")
        if not key:
            raise SystemExit(".env에 MASSIVE_API_KEY 값이 없습니다")
        download(raw, key, start, end)
    now = datetime.now(timezone.utc)
    rows = [summarize(s, raw, now) for s in SYMBOLS if (raw / f"{s}.prices.json").exists()]
    all_counts = [c for r in rows for c in r["_all"]]
    big_counts = [c for r in rows for c in r["_big"]]
    pooled = {
        "ticker_days": len(all_counts),
        "per_day_median": statistics.median(all_counts) if all_counts else None,
        "days_with_article_pct": pct(sum(c > 0 for c in all_counts), len(all_counts)),
        "ticker_big_days": len(big_counts),
        "big_median": statistics.median(big_counts) if big_counts else None,
        "big_with_article_pct": pct(sum(c > 0 for c in big_counts), len(big_counts)),
        "big_over5_pct": pct(sum(c > 5 for c in big_counts), len(big_counts)),
    }
    for r in rows:
        del r["_all"], r["_big"]
    out = {"note": "기사 수 집계만 둡니다. 시세와 기사 내용은 data/raw/에 있고 저장소에 올리지 않습니다.",
           "surveyed": end, "from": start, "pooled": pooled, "rows": rows}
    if "--raw" not in sys.argv:
        (HERE / out_name).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("| 종목 | 거래일 | 기사 | 기사 있는 날 | 하루 중앙값 | 큰 움직임 날 | 그중 기사 있음 | 그날 중앙값 | 5건 넘음 | 기사당 종목 수 | 종목 3개 이하 | 종목 감성 있음 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['symbol']} | {r['trading_days']} | {r['articles']:,} | {r['days_with_article_pct']}% | {r['per_day_median']} | "
              f"{r['big_days']} | {r['big_with_article_pct']}% | {r['big_median']} | {r['big_over5_pct']}% | "
              f"{r['tickers_per_article_median']} | {r['focused_pct']}% | {r['with_insight_pct']}% |")
    print("\n합친 값:", json.dumps(pooled, ensure_ascii=False))


if __name__ == "__main__":
    main()
