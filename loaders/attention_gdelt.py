#!/usr/bin/env python3
"""GDELT DOC 2.0 API에서 주제어가 나온 기사 수, 비율, 평균 어조를 받아 source.attention_daily(provider=gdelt)에 넣습니다.

- metric: article_count(기사 수), article_share(감시한 전체 기사 가운데 %), tone(기사 수로 가중한 평균 어조).
- DOC API는 최근 약 3개월만 찾습니다. 그보다 오래된 날은 원본 파일(GKG)이나 BigQuery 공개 데이터셋이 필요합니다.
- 5초에 1번까지라 호출 사이에 6초 쉽니다. 429가 오면 기다렸다 다시 하고, 계속 막히면 남은 주제를 건너뜁니다.
- 늦게 색인된 기사로 최근 며칠 값이 늘 수 있어, 매일 최근 며칠을 다시 받아 바뀐 행만 고칩니다.
- 날짜는 UTC입니다. GDELT를 쓴 결과에는 GDELT 출처를 적습니다.

예:
  python loaders/attention_gdelt.py --days 90
  python loaders/attention_gdelt.py --topics rates --days 7 --dry-run
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

from attention import load, summary, topics
from common import FetchError, Http, date_args, load_env, new_parser, read_raw, resolve_dates, save_raw

API = "https://api.gdeltproject.org/api/v2/doc/doc"
STOP_AFTER = 2  # 이만큼 주제가 연달아 막히면 멈춤


def call(http, query, mode, start, end):
    params = {"query": query, "mode": mode, "format": "json",
              "startdatetime": f"{start:%Y%m%d}000000", "enddatetime": f"{end + timedelta(days=1):%Y%m%d}000000"}
    status, data, body = http.json(f"{API}?{urlencode(params)}")
    if status != 200 or data is None:
        raise FetchError(http.describe(status, body))
    return data


def points(data):
    out = []
    for series in data.get("timeline") or []:
        for pt in series.get("data") or []:
            out.append((datetime.strptime(pt["date"][:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc),
                        pt.get("value"), pt.get("norm")))
    return out


def daily(vol, tone, start, end):
    """15분이나 하루 단위로 오는 점을 UTC 날짜로 묶습니다."""
    count, norm, tone_w, tone_n = defaultdict(int), defaultdict(int), defaultdict(float), defaultdict(int)
    weight = {}
    for ts, v, n in vol:
        count[ts.date()] += int(v or 0)
        norm[ts.date()] += int(n or 0)
        weight[ts] = int(v or 0)
    for ts, v, _ in tone:
        w = weight.get(ts, 0)
        if v is not None and w:
            tone_w[ts.date()] += float(v) * w
            tone_n[ts.date()] += w
    rows = {}
    for d in count:
        if not start <= d <= end:
            continue
        rows[(d, "article_count")] = Decimal(count[d])
        if norm[d]:
            rows[(d, "article_share")] = (Decimal(count[d]) * 100 / Decimal(norm[d])).quantize(Decimal("0.0001"))
        if tone_n[d]:
            rows[(d, "tone")] = Decimal(str(round(tone_w[d] / tone_n[d], 4)))
    return rows


def main():
    p = new_parser(__doc__)
    date_args(p, default_days=7)
    p.add_argument("--topics", help="쉼표로 구분한 topic_id만")
    args = p.parse_args()
    start, end = resolve_dates(args, datetime.now(timezone.utc).date())
    env = load_env()
    http = Http(min_interval=6.0, wait_429=10, tries_429=3)
    chosen = topics(args.topics.split(",") if args.topics else None)
    print(f"기간(UTC) {start} ~ {end}, 주제 {len(chosen)}개" + (", 시험(DB에 쓰지 않음)" if args.dry_run else ""))
    rows, failures, blocked = [], [], 0
    for t in chosen:
        if blocked >= STOP_AFTER:
            failures.append(f"{t['topic_id']}: 앞 주제가 연달아 막혀 건너뜀")
            continue
        name = f"{t['topic_id']}_{start:%Y%m%d}_{end:%Y%m%d}"
        try:
            if args.from_raw:
                raw = read_raw("attention_gdelt", name)
            else:
                raw = {"query": t["gdelt_query"],
                       "timelinevolraw": call(http, t["gdelt_query"], "timelinevolraw", start, end),
                       "timelinetone": call(http, t["gdelt_query"], "timelinetone", start, end)}
                save_raw("attention_gdelt", name, raw)
        except FetchError as e:
            failures.append(f"{t['topic_id']}: {e}")
            blocked += 1
            continue
        blocked = 0
        got = daily(points(raw["timelinevolraw"]), points(raw["timelinetone"]), start, end)
        rows += [(t["topic_id"], d, "gdelt", m, v) for (d, m), v in sorted(got.items())]
    print(f"호출 {http.calls}번, 받은 행 {len(rows)}개")
    if rows:
        summary(rows)
    for f in failures:
        print(f"실패: {f}")
    if rows and not args.dry_run:
        load(env, rows)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
