#!/usr/bin/env python3
"""한국은행 ECOS의 일별 원/달러 매매기준율을 받아 source.fx_rates에 넣습니다(값이 바뀐 행만 고침).

통계표 731Y001(주요국 통화의 대원화환율), 항목 0000001(원/미국달러 매매기준율)입니다.
rate_date는 고시한 한국 날짜이고, 한국 휴일에는 행이 없습니다. 미국 거래일과 맞추는 규칙은
아직 정하지 않았습니다(docs/ai/data-prep.md). 받은 원본은 data/raw/fx_rates/에 둡니다.

예:
  python loaders/fx_rates.py --days 365          처음 1년치
  python loaders/fx_rates.py                     그 뒤 매일(최근 7일)
  python loaders/fx_rates.py --start 2026-10-01 --end 2026-10-31 --dry-run

ECOS 자료를 화면이나 글에 보일 때는 한국은행 경제통계시스템(ECOS)에서 제공된 정보임을 표시합니다.
"""
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from common import (FetchError, Http, connect, date_args, load_env, need, new_parser, read_raw, resolve_dates,
                    save_raw, upsert, utc_now)

ECOS = "https://ecos.bok.or.kr/api/StatisticSearch"
SERIES = {"USDKRW": ("731Y001", "0000001")}
KST = ZoneInfo("Asia/Seoul")

UPSERT = """
INSERT INTO source.fx_rates AS f
  (currency_pair, rate_date, rate, provider, loaded_at)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (currency_pair, rate_date)
DO UPDATE SET
  rate      = EXCLUDED.rate,
  provider  = EXCLUDED.provider,
  loaded_at = EXCLUDED.loaded_at
WHERE (f.rate, f.provider) IS DISTINCT FROM (EXCLUDED.rate, EXCLUDED.provider)
RETURNING (xmax = 0)
"""


def fetch(http, key, table, item, start, end):
    rows, first, size = [], 1, 1000
    while True:
        url = f"{ECOS}/{key}/json/kr/{first}/{first + size - 1}/{table}/D/{start:%Y%m%d}/{end:%Y%m%d}/{item}"
        status, data, body = http.json(url)
        if status != 200 or data is None:
            raise FetchError(http.describe(status, body))
        if "RESULT" in data:
            code, msg = data["RESULT"].get("CODE", ""), data["RESULT"].get("MESSAGE", "")
            if code == "INFO-200":  # 해당하는 데이터가 없음
                return rows
            raise FetchError(http.mask(f"ECOS {code} {msg}"))
        block = data.get("StatisticSearch") or {}
        rows += block.get("row") or []
        if first + size - 1 >= int(block.get("list_total_count") or 0):
            return rows
        first += size


def main():
    p = new_parser(__doc__)
    date_args(p, default_days=7)
    args = p.parse_args()
    start, end = resolve_dates(args, datetime.now(KST).date())
    env = load_env()
    key = "" if args.from_raw else need(env, "ECOS_API_KEY")
    http = Http(secrets=[key], min_interval=0.5)
    print(f"기간 {start} ~ {end}" + (", 시험(DB에 쓰지 않음)" if args.dry_run else ""))
    out, failures = [], []
    for pair, (table, item) in SERIES.items():
        name = f"{pair}_{start:%Y%m%d}_{end:%Y%m%d}"
        try:
            raw = read_raw("fx_rates", name) if args.from_raw else fetch(http, key, table, item, start, end)
        except FetchError as e:
            failures.append(f"{pair}: {e}")
            continue
        if not args.from_raw:
            save_raw("fx_rates", name, raw)
        for r in raw:
            value = (r.get("DATA_VALUE") or "").replace(",", "")
            if not value:
                continue
            out.append((pair, datetime.strptime(r["TIME"], "%Y%m%d").date(), Decimal(value), "ecos"))
    dates = [r[1] for r in out]
    print(f"호출 {http.calls}번, 받은 행 {len(out)}개, {min(dates) if dates else '-'} ~ {max(dates) if dates else '-'}")
    for f in failures:
        print(f"실패: {f}")
    if args.dry_run or not out:
        return 1 if failures else 0
    loaded_at = utc_now()
    with connect(env) as conn:
        ins, upd, same = upsert(conn, UPSERT, [r + (loaded_at,) for r in out])
    print(f"DB: 새로 넣음 {ins}, 고침 {upd}, 그대로 {same}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
