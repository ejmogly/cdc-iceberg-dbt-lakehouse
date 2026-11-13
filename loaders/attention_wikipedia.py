#!/usr/bin/env python3
"""위키백과(영어) 문서별 하루 조회수를 받아 source.attention_daily(provider=wikipedia, metric=pageviews)에 넣습니다.

Wikimedia REST API는 키가 없지만, 프로그램 이름과 연락처가 든 User-Agent를 요구합니다.
연락처는 .env의 WIKIMEDIA_CONTACT에서 읽습니다(공개 저장소에 적어도 되는 메일 주소나 웹 주소).
사람의 조회(agent=user)만 셉니다. 날짜는 UTC입니다. 한 주제에 문서가 여럿이면 더합니다.

예:
  python loaders/attention_wikipedia.py --days 365
  python loaders/attention_wikipedia.py --topics rates,oil --days 30 --dry-run
"""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

from attention import load, summary, topics
from common import USER_AGENT, FetchError, Http, date_args, load_env, need, new_parser, read_raw, resolve_dates, save_raw

API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia.org/all-access/user"


def main():
    p = new_parser(__doc__)
    date_args(p, default_days=7)
    p.add_argument("--topics", help="쉼표로 구분한 topic_id만")
    args = p.parse_args()
    start, end = resolve_dates(args, datetime.now(timezone.utc).date())
    env = load_env()
    contact = "" if args.from_raw else need(env, "WIKIMEDIA_CONTACT")
    http = Http(secrets=[contact], min_interval=1.0, wait_429=30, user_agent=f"{USER_AGENT} ({contact})")
    chosen = topics(args.topics.split(",") if args.topics else None)
    print(f"기간(UTC) {start} ~ {end}, 주제 {len(chosen)}개" + (", 시험(DB에 쓰지 않음)" if args.dry_run else ""))
    rows, failures = [], []
    for t in chosen:
        views = {}
        for article in t["wiki_articles"]:
            name = f"{article}_{start:%Y%m%d}_{end:%Y%m%d}"
            try:
                if args.from_raw:
                    data = read_raw("attention_wikipedia", name)
                else:
                    status, data, body = http.json(f"{API}/{quote(article, safe='')}/daily/{start:%Y%m%d}/{end:%Y%m%d}")
                    if status != 200 or data is None:
                        raise FetchError(http.describe(status, body))
                    save_raw("attention_wikipedia", name, data)
            except FetchError as e:
                failures.append(f"{t['topic_id']} {article}: {e}")
                continue
            for it in data.get("items") or []:
                d = datetime.strptime(it["timestamp"][:8], "%Y%m%d").date()
                views[d] = views.get(d, 0) + int(it["views"])
        rows += [(t["topic_id"], d, "wikipedia", "pageviews", Decimal(v)) for d, v in sorted(views.items())]
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
