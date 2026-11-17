#!/usr/bin/env python3
"""미국 일정(FOMC 금리 결정, CPI 발표, 고용 보고서 발표)을 받아 data/raw/schedule/에 원본과 정리본을 둡니다.

E02 「그때 무슨 일이」의 근거(일정 표)입니다. 출처는 설계서 6절 5번(2026-11-13)과 데이터 준비 문서 6절입니다.
- FOMC: 연준 FOMC 일정 페이지(HTML, 키 없음, 미국 정부 저작물). 정례 회의의 둘째 날(결정과 성명 발표일)을 씁니다.
  성명서 링크(monetaryYYYYMMDDa)가 있는 회의는 링크의 날짜와 맞는지 검사합니다. 서면 결의(notation vote)는
  금리 결정 회의가 아니라 넣지 않고 따로 셉니다. 발표 시각은 미국 동부 14:00입니다.
- CPI, 고용 보고서: FRED API `fred/release/dates`(release_id 10, 50, 무료 키 FRED_API_KEY). 앞으로 잡힌 날짜도
  받습니다(include_release_dates_with_no_data). FRED는 날짜만 주므로 시각은 BLS 발표 시각(미국 동부 08:30)으로 둡니다.
  This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.
- 국내 일정(한국은행, 국가데이터처)은 이번 판에서 받지 않습니다(E02 1판이 국내 종목을 뺌, 2026-11-17 담당자 결정).

정리본 data/raw/schedule/events_{시작}_{끝}.json은 ai/screen_data.py가 화면 파일의 events 테이블로 내보냅니다
(원천 DB에 넣지 않음. 설계서 6절 6번의 기사와 같은 길).

예:
  python loaders/schedule_events.py                   FOMC와 FRED(키가 없으면 FOMC만 받고 FRED는 건너뜀을 알림)
  python loaders/schedule_events.py --from-raw        받은 원본으로 정리본만 다시 만듦
"""
import re
import sys
from datetime import date

from common import RAW, FetchError, Http, load_env, new_parser, read_raw, report, save_raw, utc_now

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED_URL = "https://api.stlouisfed.org/fred"
FRED_RELEASES = {"cpi": (10, "미국 소비자물가지수(CPI) 발표"), "jobs": (50, "미국 고용 보고서 발표")}
START, END = date(2024, 11, 13), date(2026, 12, 31)  # 화면 데이터 기간(2024-11-13 ~ 2026-11-12)과 그 뒤 잡힌 일정
TAG = f"{START:%Y%m%d}_{END:%Y%m%d}"
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})


def parse_fomc(html):
    """(정례 회의 결정일 목록, 서면 결의 목록, 검사 결과). 결정일은 {"date", "sep", "statement_date"}."""
    meetings, notations, problems = [], [], []
    panels = re.split(r'<div class="panel-heading"><h4><a id="\d+">(\d{4}) FOMC Meetings</a></h4></div>', html)
    for year, body in zip(panels[1::2], panels[2::2]):
        year = int(year)
        rows = re.split(r'<div class="(?:fomc-meeting--shaded )?row fomc-meeting', body)[1:]
        for row in rows:
            month = re.search(r'fomc-meeting__month[^>]*>\s*(?:<strong>)?([^<]+)', row)
            day = re.search(r'fomc-meeting__date[^>]*>\s*([^<]+)', row)
            if not (month and day):
                problems.append(f"{year}년 회의 줄에서 달이나 날짜를 읽지 못함")
                continue
            month, day = month.group(1).strip(), " ".join(day.group(1).split())
            if "notation vote" in day or "unscheduled" in day.lower():
                notations.append({"year": year, "month": month, "day": day})
                continue
            m = re.fullmatch(r"(\d{1,2})-(\d{1,2})(\*?)", day)
            if not m:
                problems.append(f"{year} {month} {day!r}: 날짜 모양을 모름")
                continue
            last_month = month.split("/")[-1]  # Apr/May 30-1 → 둘째 날은 5월
            d = date(year, MONTHS[last_month], int(m.group(2)))
            st = re.search(r"monetary(\d{8})a1?\.(?:pdf|htm)", row)
            st_date = date.fromisoformat(f"{st.group(1)[:4]}-{st.group(1)[4:6]}-{st.group(1)[6:]}") if st else None
            if st_date and st_date != d:
                problems.append(f"{d}: 성명서 링크 날짜 {st_date}와 다름")
            meetings.append({"date": d.isoformat(), "sep": m.group(3) == "*",
                             "statement_date": st_date.isoformat() if st_date else None})
    return meetings, notations, problems


def fetch_fomc(http):
    status, body = http.request(FOMC_URL)
    if status != 200:
        raise FetchError(http.describe(status, body))
    html = body.decode("utf-8", "replace")
    save_raw("schedule", f"fomc_calendars_{utc_now():%Y%m%d}", {"url": FOMC_URL, "fetched_at": utc_now().isoformat(),
                                                               "html": html})
    return html


def fetch_fred(http, key):
    out = {}
    for kind, (rid, _) in FRED_RELEASES.items():
        url = (f"{FRED_URL}/release/dates?release_id={rid}&api_key={key}&file_type=json&sort_order=asc"
               f"&realtime_start={START}&realtime_end=9999-12-31&include_release_dates_with_no_data=true&limit=10000")
        status, parsed, body = http.json(url)
        if status != 200 or not parsed:
            raise FetchError(http.describe(status, body))
        name_status, name, nbody = http.json(f"{FRED_URL}/release?release_id={rid}&api_key={key}&file_type=json")
        if name_status != 200 or not name:
            raise FetchError(http.describe(name_status, nbody))
        out[kind] = {"release_id": rid, "release": name.get("releases", [{}])[0].get("name"),
                     "dates": [r["date"] for r in parsed.get("release_dates", [])]}
    save_raw("schedule", f"fred_release_dates_{utc_now():%Y%m%d}", {"fetched_at": utc_now().isoformat(), **out})
    return out


def build(fomc, fred):
    """정리본: 기간 안의 일정. id는 종류와 날짜, 시각과 시간대를 함께 둡니다(FOMC는 한국 시각으로 다음 날 새벽)."""
    events = []
    for m in fomc:
        if START.isoformat() <= m["date"] <= END.isoformat():
            events.append({"id": f"fomc-{m['date']}", "date": m["date"], "kind": "fomc", "local_time": "14:00",
                           "tz": "America/New_York", "source": "Federal Reserve FOMC calendar",
                           "title": "FOMC 금리 결정 발표" + ("(경제 전망 함께 발표)" if m["sep"] else "")})
    for kind, v in (fred or {}).items():
        title = FRED_RELEASES[kind][1]
        for d in v["dates"]:
            if START.isoformat() <= d <= END.isoformat():
                events.append({"id": f"{kind}-{d}", "date": d, "kind": kind, "local_time": "08:30",
                               "tz": "America/New_York", "source": f"FRED release {v['release_id']} ({v['release']})",
                               "title": title})
    return sorted(events, key=lambda e: (e["date"], e["id"]))


def main():
    p = new_parser(__doc__)
    p.add_argument("--from-raw", metavar="YYYYMMDD", help="그날 받은 원본으로 정리본만 다시 만듦")
    args = p.parse_args()
    env = load_env()
    key = env.get("FRED_API_KEY", "")
    http = Http(secrets=[key], min_interval=1.0)
    if args.from_raw:
        html = read_raw("schedule", f"fomc_calendars_{args.from_raw}")["html"]
        fred_path = RAW / "schedule" / f"fred_release_dates_{args.from_raw}.json"
        fred = read_raw("schedule", f"fred_release_dates_{args.from_raw}") if fred_path.exists() else None
        fred = {k: v for k, v in (fred or {}).items() if k in FRED_RELEASES} or None
    else:
        html = fetch_fomc(http)
        fred = fetch_fred(http, key) if key else None
    meetings, notations, problems = parse_fomc(html)
    if problems:
        sys.exit("FOMC 페이지 검사 실패:\n  " + "\n  ".join(problems))
    events = build(meetings, fred)
    path = save_raw("schedule", f"events_{TAG}", {
        "note": "E02 일정 표. 출처: 연준 FOMC 일정 페이지(공공 영역), FRED API(St. Louis Fed가 보증하지 않음)",
        "range": [START.isoformat(), END.isoformat()], "fred": bool(fred), "events": events})
    kinds = {k: sum(e["kind"] == k for e in events) for k in ("fomc", "cpi", "jobs")}
    checked = sum(1 for m in meetings if m["statement_date"])
    report("일정", [f"FOMC 정례 회의 {len(meetings)}개(성명서 링크 날짜와 대조 {checked}개 모두 같음), 서면 결의 {len(notations)}개 뺌",
                    f"기간 안 일정: FOMC {kinds['fomc']}, CPI {kinds['cpi']}, 고용 {kinds['jobs']}",
                    "FRED: " + ("받음" if fred else ".env에 FRED_API_KEY가 없어 건너뜀(CPI와 고용 일정 없음)"),
                    f"정리본: {path.relative_to(RAW.parent.parent)}"])


if __name__ == "__main__":
    main()
