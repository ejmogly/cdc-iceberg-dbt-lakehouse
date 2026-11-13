#!/usr/bin/env python3
"""Massive 종목 뉴스의 제목, 요약, 종목별 감성을 받아 source.news_items, source.news_ticker_sentiment에 넣습니다.

기사 본문은 받지도 남기지도 않습니다(기사 주소만 남기고 그 주소를 열지 않습니다). 받은 글은 재배포하지 않습니다.
시세와 같은 무료 요금제(분당 5회, 개인 비상업 용도)라 시세 적재기와 동시에 돌리지 않습니다.
기간은 UTC 날짜입니다. 받은 원본은 data/raw/news_massive/에 둡니다.

예:
  python loaders/news_massive.py --days 2                       미국 17종목에 표시된 기사(그 뒤 매일)
  python loaders/news_massive.py --all-articles --days 1        종목 표시와 관계없이 그 기간의 전체 기사(E02 검색 대상)
  python loaders/news_massive.py --tickers SPY,QQQ --start 2026-11-01 --end 2026-11-07 --dry-run
"""
import csv
import sys
from datetime import datetime, timezone

from common import (ROOT, FetchError, Http, connect, date_args, load_env, need, new_parser, read_raw,
                    resolve_dates, save_raw, upsert, utc_now)

MASSIVE = "https://api.massive.com"

UPSERT_ITEM = """
INSERT INTO source.news_items AS n
  (article_id, published_at, publisher, title, description, article_url, tickers, keywords, provider, loaded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (article_id)
DO UPDATE SET
  published_at = EXCLUDED.published_at, publisher = EXCLUDED.publisher, title = EXCLUDED.title,
  description = EXCLUDED.description, article_url = EXCLUDED.article_url, tickers = EXCLUDED.tickers,
  keywords = EXCLUDED.keywords, loaded_at = EXCLUDED.loaded_at
WHERE (n.published_at, n.publisher, n.title, n.description, n.article_url, n.tickers, n.keywords)
  IS DISTINCT FROM
      (EXCLUDED.published_at, EXCLUDED.publisher, EXCLUDED.title, EXCLUDED.description,
       EXCLUDED.article_url, EXCLUDED.tickers, EXCLUDED.keywords)
RETURNING (xmax = 0)
"""

UPSERT_SENTIMENT = """
INSERT INTO source.news_ticker_sentiment AS s
  (article_id, ticker, sentiment, sentiment_reasoning, provider, loaded_at)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (article_id, ticker)
DO UPDATE SET
  sentiment = EXCLUDED.sentiment, sentiment_reasoning = EXCLUDED.sentiment_reasoning,
  loaded_at = EXCLUDED.loaded_at
WHERE (s.sentiment, s.sentiment_reasoning) IS DISTINCT FROM (EXCLUDED.sentiment, EXCLUDED.sentiment_reasoning)
RETURNING (xmax = 0)
"""


def us_symbols():
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8") as f:
        return [r["symbol"] for r in csv.DictReader(f) if r["market"] == "US"]


def fetch(http, key, ticker, start, end, max_pages):
    """(받은 쪽 목록, 쪽 수 상한에 걸렸는지)."""
    query = (f"published_utc.gte={start}T00:00:00Z&published_utc.lte={end}T23:59:59Z"
             "&order=asc&sort=published_utc&limit=1000")
    url = f"{MASSIVE}/v2/reference/news?{'ticker=' + ticker + '&' if ticker else ''}{query}"
    pages = []
    while url and len(pages) < max_pages:
        status, data, body = http.json(url, {"Authorization": f"Bearer {key}"})
        if status != 200 or data is None or data.get("status") not in ("OK", "DELAYED"):
            raise FetchError(http.describe(status, body))
        pages.append(data)
        url = data.get("next_url")
    return pages, bool(url)


def parse(pages):
    """본문이 아닌 칸만 고릅니다. 같은 기사가 여러 종목에서 오면 하나로 합칩니다."""
    items, sentiments = {}, {}
    for page in pages:
        for it in page.get("results") or []:
            published = datetime.fromisoformat(it["published_utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
            items[it["id"]] = (it["id"], published, (it.get("publisher") or {}).get("name"), it.get("title") or "",
                               it.get("description"), it.get("article_url"), sorted(set(it.get("tickers") or [])),
                               sorted(set(it.get("keywords") or [])), "massive")
            for ins in it.get("insights") or []:
                if ins.get("ticker") and ins.get("sentiment"):
                    sentiments[(it["id"], ins["ticker"])] = (it["id"], ins["ticker"], ins["sentiment"],
                                                             ins.get("sentiment_reasoning"), "massive")
    return items, sentiments


def main():
    p = new_parser(__doc__)
    date_args(p, default_days=2)
    p.add_argument("--tickers", help="쉼표로 구분. 기본은 instruments.csv의 미국 17종목")
    p.add_argument("--all-articles", action="store_true", help="종목 조건 없이 그 기간의 전체 기사")
    p.add_argument("--max-pages", type=int, default=50, help="요청 하나가 넘길 쪽 수 상한(한 쪽 최대 1,000건)")
    args = p.parse_args()
    start, end = resolve_dates(args, datetime.now(timezone.utc).date())
    env = load_env()
    key = "" if args.from_raw else need(env, "MASSIVE_API_KEY")
    http = Http(secrets=[key], min_interval=12.5)
    targets = [None] if args.all_articles else (args.tickers.split(",") if args.tickers else us_symbols())
    print(f"기간(UTC) {start} ~ {end}, " + ("전체 기사" if args.all_articles else f"종목 {len(targets)}개")
          + (", 시험(DB에 쓰지 않음)" if args.dry_run else ""))

    items, sentiments, failures, capped = {}, {}, [], []
    print("| 대상 | 쪽 | 기사 | 첫 기사(UTC) | 끝 기사(UTC) |")
    print("|---|---|---|---|---|")
    for t in targets:
        name = f"{t or 'ALL'}_{start:%Y%m%d}_{end:%Y%m%d}"
        try:
            if args.from_raw:
                raw = read_raw("news_massive", name)
                pages, cap = raw["pages"], raw["capped"]
            else:
                pages, cap = fetch(http, key, t, start, end, args.max_pages)
                save_raw("news_massive", name, {"capped": cap, "pages": pages})
        except FetchError as e:
            failures.append(f"{t or '전체'}: {e}")
            continue
        if cap:
            capped.append(t or "전체")
        got, sent = parse(pages)
        stamps = sorted(v[1] for v in got.values())
        print(f"| {t or '전체'} | {len(pages)} | {len(got)} | {stamps[0]:%Y-%m-%d %H:%M} | {stamps[-1]:%Y-%m-%d %H:%M} |"
              if stamps else f"| {t or '전체'} | {len(pages)} | 0 | - | - |", flush=True)
        items.update(got)
        sentiments.update(sent)
    with_sent = len({k[0] for k in sentiments})
    print(f"\n호출 {http.calls}번, 기사 {len(items)}건(중복 제거), 종목 감성 {len(sentiments)}개(기사 {with_sent}건)")
    if capped:
        print(f"쪽 수 상한에 걸려 덜 받은 대상: {', '.join(capped)}")
    for f in failures:
        print(f"실패: {f}")
    if args.dry_run or not items:
        return 1 if failures else 0
    loaded_at = utc_now()
    with connect(env) as conn:
        a = upsert(conn, UPSERT_ITEM, [v + (loaded_at,) for v in items.values()])
        b = upsert(conn, UPSERT_SENTIMENT, [v + (loaded_at,) for v in sentiments.values()])
    print(f"DB 기사: 새로 넣음 {a[0]}, 고침 {a[1]}, 그대로 {a[2]}")
    print(f"DB 감성: 새로 넣음 {b[0]}, 고침 {b[1]}, 그대로 {b[2]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
