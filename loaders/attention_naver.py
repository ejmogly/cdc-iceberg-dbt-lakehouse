#!/usr/bin/env python3
"""네이버 데이터랩 검색어 트렌드의 하루 상대값을 받아 source.attention_daily(provider=naver, metric=search_index)에 넣습니다.

.env에 NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET이 있을 때만 씁니다. 없으면 아무것도 하지 않고 끝납니다.
값은 요청한 기간 안의 최댓값을 100으로 맞춘 상대값이라, 기간이나 함께 요청한 주제가 바뀌면 지난 값도 바뀝니다.
그래서 주제마다 따로 요청하고, 요청 시작일을 --base-start로 고정하고, 매번 그 기간 전체를 다시 넣습니다.
끝일이 늘어 최댓값이 바뀌면 지난 날짜 값이 고쳐지고(CDC u 이벤트), 이것이 이 출처의 성질입니다.
날짜는 한국 날짜입니다. 받은 값은 재배포하지 않습니다(네이버 오픈API 약관).

예:
  python loaders/attention_naver.py
  python loaders/attention_naver.py --topics rates --dry-run
"""
import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from attention import load, summary, topics
from common import FetchError, Http, load_env, new_parser, read_raw, save_raw

API = "https://openapi.naver.com/v1/datalab/search"
KST = ZoneInfo("Asia/Seoul")


def main():
    p = new_parser(__doc__)
    p.add_argument("--base-start", type=date.fromisoformat, default=date(2025, 1, 1),
                   help="요청 시작일(고정). 바꾸면 모든 값이 바뀝니다. 기본 2025-01-01")
    p.add_argument("--end", type=date.fromisoformat, help="끝일. 기본은 어제(한국)")
    p.add_argument("--topics", help="쉼표로 구분한 topic_id만")
    p.add_argument("--dry-run", action="store_true", help="받아서 원본만 저장하고 DB에는 쓰지 않음")
    p.add_argument("--from-raw", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    env = load_env()
    cid, secret = env.get("NAVER_CLIENT_ID", ""), env.get("NAVER_CLIENT_SECRET", "")
    if not args.from_raw and not (cid and secret):
        print("NAVER_CLIENT_ID, NAVER_CLIENT_SECRET이 .env에 없어 건너뜁니다")
        return 0
    end = args.end or date.fromordinal(datetime.now(KST).date().toordinal() - 1)
    start = args.base_start
    http = Http(secrets=[cid, secret], min_interval=0.2)
    chosen = topics(args.topics.split(",") if args.topics else None)
    print(f"기간(한국) {start} ~ {end}, 주제 {len(chosen)}개" + (", 시험(DB에 쓰지 않음)" if args.dry_run else ""))
    rows, failures = [], []
    for t in chosen:
        name = f"{t['topic_id']}_{start:%Y%m%d}_{end:%Y%m%d}"
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "date",
                "keywordGroups": [{"groupName": t["topic_id"], "keywords": t["naver_keywords"]}]}
        try:
            if args.from_raw:
                data = read_raw("attention_naver", name)
            else:
                status, data, raw = http.json(API, {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret,
                                                    "Content-Type": "application/json"},
                                              data=json.dumps(body).encode("utf-8"), method="POST")
                if status != 200 or data is None:
                    raise FetchError(http.describe(status, raw))
                save_raw("attention_naver", name, {"request": body, "response": data})
                data = {"request": body, "response": data}
        except FetchError as e:
            failures.append(f"{t['topic_id']}: {e}")
            continue
        for res in data["response"].get("results") or []:
            for pt in res.get("data") or []:
                rows.append((t["topic_id"], date.fromisoformat(pt["period"]), "naver", "search_index",
                             Decimal(str(pt["ratio"])).quantize(Decimal("0.0001"))))
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
