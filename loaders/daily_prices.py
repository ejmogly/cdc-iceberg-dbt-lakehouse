#!/usr/bin/env python3
"""일별 시세를 받아 source.daily_prices에 넣습니다(값이 바뀐 행만 고침).

국내 2종목은 공공데이터포털 금융위원회_주식시세정보, 미국 17종목은 Massive 무료 요금제(분당 5회)에서 받습니다.
trade_date는 그 시장의 현지 날짜입니다. 받은 원본은 data/raw/daily_prices/에 둡니다.

예:
  python loaders/daily_prices.py --days 365 --adjusted false            처음 1년치
  python loaders/daily_prices.py --adjusted false                        그 뒤 매일(최근 7일을 다시 받아 바뀐 행만 고침)
  python loaders/daily_prices.py --market kr --start 2026-11-03 --end 2026-11-07 --dry-run

미국 시세의 수정주가(--adjusted)는 2026-11-13에 정했습니다: false는 원천 시세로 넣고, true(수정 종가)는 --dry-run으로 원본만 받아 화면 파일에 씁니다(data/README.md). 한 원천 테이블에 두 판을 섞지 않으려고 매번 고르게 둡니다.
국내 시세는 거래일 다음 영업일 오후 1시 이후, 미국 시세는 미국 장 마감 뒤에 나옵니다.
"""
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from common import (ROOT, FetchError, Http, connect, date_args, load_env, need, new_parser, read_raw,
                    resolve_dates, save_raw, upsert, utc_now)

FSC_URL = "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
MASSIVE = "https://api.massive.com"
ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

# 설계 초안 3절의 upsert. 세는 데만 RETURNING을 더했습니다.
UPSERT = """
INSERT INTO source.daily_prices AS p
  (symbol, trade_date, open_price, high_price, low_price, close_price, volume, provider, loaded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, trade_date)
DO UPDATE SET
  open_price  = EXCLUDED.open_price,
  high_price  = EXCLUDED.high_price,
  low_price   = EXCLUDED.low_price,
  close_price = EXCLUDED.close_price,
  volume      = EXCLUDED.volume,
  loaded_at   = EXCLUDED.loaded_at
WHERE (p.open_price, p.high_price,
       p.low_price, p.close_price,
       p.volume)
  IS DISTINCT FROM
      (EXCLUDED.open_price,
       EXCLUDED.high_price,
       EXCLUDED.low_price,
       EXCLUDED.close_price,
       EXCLUDED.volume)
RETURNING (xmax = 0)
"""


def instruments():
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def check(row):
    """값이 이상한 행의 사유. 적재는 하고 건수만 알립니다."""
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    if min(o, h, l, c) <= 0:
        return "0 이하 가격"
    if l > min(o, c) or h < max(o, c):
        return "고가, 저가 범위 밖"
    return None


# 국내: 공공데이터포털


def fetch_kr(http, key, code, start, end):
    """beginBasDt는 그날 포함, endBasDt는 그날 제외라서 끝일 다음 날을 넘깁니다."""
    items, page = [], 1
    while True:
        query = urlencode({"resultType": "json", "numOfRows": 1000, "pageNo": page, "likeSrtnCd": code,
                           "beginBasDt": start.strftime("%Y%m%d"),
                           "endBasDt": (end + timedelta(days=1)).strftime("%Y%m%d")})
        status, data, body = http.json(f"{FSC_URL}?serviceKey={key}&{query}")
        resp = (data or {}).get("response") or {}
        head, bodyj = resp.get("header") or {}, resp.get("body")
        if status != 200 or bodyj is None or head.get("resultCode") not in (None, "00"):
            raise FetchError(http.describe(status, body))
        got = (bodyj.get("items") or {}).get("item") or []
        items += got
        if page * 1000 >= int(bodyj.get("totalCount") or 0) or not got:
            return items
        page += 1


def parse_kr(code, items, start, end):
    rows = []
    for it in items:
        if it.get("srtnCd") != code:
            continue
        d = datetime.strptime(it["basDt"], "%Y%m%d").date()
        if not start <= d <= end:
            continue
        rows.append({"symbol": code, "trade_date": d, "open": Decimal(it["mkp"]), "high": Decimal(it["hipr"]),
                     "low": Decimal(it["lopr"]), "close": Decimal(it["clpr"]), "volume": int(it["trqu"]),
                     "provider": "data.go.kr"})
    return rows


# 미국: Massive


def massive_get(http, key, path):
    status, data, body = http.json(f"{MASSIVE}{path}", {"Authorization": f"Bearer {key}"})
    if status != 200 or data is None or data.get("status") not in ("OK", "DELAYED"):
        raise FetchError(http.describe(status, body))
    return data


def bar_row(sym, r, fractional):
    v = Decimal(str(r["v"]))
    if v != v.to_integral_value():
        fractional[sym] += 1
    return {"symbol": sym, "trade_date": datetime.fromtimestamp(r["t"] / 1000, timezone.utc).astimezone(ET).date(),
            "open": Decimal(str(r["o"])), "high": Decimal(str(r["h"])), "low": Decimal(str(r["l"])),
            "close": Decimal(str(r["c"])), "volume": int(v.to_integral_value()), "provider": "massive"}


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def main():
    p = new_parser(__doc__)
    date_args(p, default_days=7)
    p.add_argument("--market", choices=["kr", "us", "all"], default="all")
    p.add_argument("--symbols", help="쉼표로 구분한 일부 종목만")
    p.add_argument("--adjusted", choices=["true", "false"], help="미국 시세의 수정주가 여부. 원천에는 false, 수정 종가는 true --dry-run(data/README.md)")
    p.add_argument("--mode", choices=["auto", "ticker", "grouped"], default="auto",
                   help="미국: ticker=종목마다 기간 조회, grouped=날짜마다 전 종목 조회. auto는 평일 5일 넘으면 ticker")
    args = p.parse_args()
    start, end = resolve_dates(args, datetime.now(KST).date())
    env = load_env()
    wanted = set(args.symbols.split(",")) if args.symbols else None
    inst = [i for i in instruments() if wanted is None or i["symbol"] in wanted]
    kr = [i["symbol"] for i in inst if i["market"] == "KRX" and args.market in ("kr", "all")]
    us = [i["symbol"] for i in inst if i["market"] == "US" and args.market in ("us", "all")]
    if us and not args.adjusted:
        sys.exit("미국 시세는 --adjusted true 또는 false를 골라야 합니다(원천에는 false, 수정 종가는 true --dry-run, data/README.md)")
    print(f"기간 {start} ~ {end}, 국내 {len(kr)}종목, 미국 {len(us)}종목"
          + (f", adjusted={args.adjusted}" if us else "") + (", 시험(DB에 쓰지 않음)" if args.dry_run else ""))

    rows, failures, fractional, calls = [], [], defaultdict(int), 0
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"
    if kr:
        key = "" if args.from_raw else need(env, "DATA_GO_KR_SERVICE_KEY")
        http = Http(secrets=[key], min_interval=0.5)
        if key and "%" not in key:
            key = quote(key, safe="")
            http.secrets.append(key)
        for code in kr:
            name = f"kr_{code}_{tag}"
            try:
                items = read_raw("daily_prices", name) if args.from_raw else fetch_kr(http, key, code, start, end)
            except FetchError as e:
                failures.append(f"{code}: {e}")
                continue
            if not args.from_raw:
                save_raw("daily_prices", name, items)
            rows += parse_kr(code, items, start, end)
        calls += http.calls

    if us:
        key = "" if args.from_raw else need(env, "MASSIVE_API_KEY")
        http = Http(secrets=[key], min_interval=12.5)
        days = list(weekdays(start, end))
        mode = args.mode if args.mode != "auto" else ("ticker" if len(days) > 5 else "grouped")
        if mode == "ticker":
            for sym in us:
                name = f"us_{sym}_{tag}_adj{args.adjusted}"
                try:
                    data = read_raw("daily_prices", name) if args.from_raw else massive_get(
                        http, key, f"/v2/aggs/ticker/{sym}/range/1/day/{start}/{end}"
                                   f"?adjusted={args.adjusted}&sort=asc&limit=50000")
                except FetchError as e:
                    failures.append(f"{sym}: {e}")
                    continue
                if not args.from_raw:
                    save_raw("daily_prices", name, data)
                rows += [bar_row(sym, r, fractional) for r in data.get("results") or []]
                print(f"  {sym} {data.get('resultsCount', 0)}행", flush=True)
        else:
            empty = []
            for d in days:
                name = f"us_grouped_{d:%Y%m%d}_adj{args.adjusted}"
                try:
                    data = read_raw("daily_prices", name) if args.from_raw else massive_get(
                        http, key, f"/v2/aggs/grouped/locale/us/market/stocks/{d}?adjusted={args.adjusted}")
                except FetchError as e:
                    failures.append(f"{d}: {e}")
                    continue
                if not args.from_raw:
                    save_raw("daily_prices", name, data)
                got = [r for r in data.get("results") or [] if r.get("T") in us]
                if not got:
                    empty.append(d.isoformat())
                rows += [bar_row(r["T"], r, fractional) for r in got]
            if empty:
                print(f"  행이 없는 평일(휴장이거나 아직 나오지 않음): {', '.join(empty)}")
        calls += http.calls

    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r["trade_date"])
    odd = defaultdict(list)
    for r in rows:
        why = check(r)
        if why:
            odd[why].append(f"{r['symbol']} {r['trade_date']}")
    print(f"\n호출 {calls}번, 받은 행 {len(rows)}개")
    print("| 종목 | 행 | 첫 날 | 끝 날 |")
    print("|---|---|---|---|")
    for sym in kr + us:
        ds = by_sym.get(sym, [])
        print(f"| {sym} | {len(ds)} | {min(ds) if ds else '-'} | {max(ds) if ds else '-'} |")
    for why, where in odd.items():
        print(f"이상한 값({why}) {len(where)}행: {', '.join(where[:5])}{' 등' if len(where) > 5 else ''}")
    if fractional:
        print(f"거래량이 정수가 아니어서 반올림한 행: {dict(fractional)}")
    for f in failures:
        print(f"실패: {f}")

    if args.dry_run or not rows:
        return 1 if failures else 0
    loaded_at = utc_now()
    params = [(r["symbol"], r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"],
               r["provider"], loaded_at) for r in rows]
    with connect(env) as conn:
        ins, upd, same = upsert(conn, UPSERT, params)
    print(f"DB: 새로 넣음 {ins}, 고침 {upd}, 그대로 {same}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
