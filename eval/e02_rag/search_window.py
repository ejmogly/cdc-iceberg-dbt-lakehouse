"""E02 검색 창(docs/ai/experiments/E02-rag.md 3절 「검색 창」). 인용 유효 채점과 검색이 같은 창을 쓰게 한 곳에 둡니다.

거래일 t의 창: 전날 장 마감부터 t 다음 날 장 시작 전(09:30)까지, 미국 동부 시각.
전날 장 마감은 보통 16:00이고, 조기 폐장(반일장)이면 13:00입니다.
step0_survey.windows()는 늘 16:00을 써서, 반일장 다음 날의 창이 세 시간 늦게 시작했습니다(2026-11-13 검토에서 찾음).
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# NYSE 조기 폐장(13:00) 날, 데이터 기간(2024-11-13 ~ 2026-11-12) 안.
# 2026-07-02는 조기 폐장인지 확인하지 못해 넣지 않았습니다. 평가 날짜 40개 가운데 그다음 거래일은 없습니다.
HALF_DAYS = {date(2024, 11, 29), date(2024, 12, 24), date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}


def window(days, t):
    """거래일 목록 days(정렬된 date)에서 t의 검색 창 (시작, 끝)을 미국 동부 시각으로 돌려줍니다."""
    i = days.index(t)
    if i == 0 or i + 1 >= len(days):
        raise ValueError(f"{t}는 앞뒤 거래일이 없어 창을 정할 수 없습니다")
    prev, nxt = days[i - 1], days[i + 1]
    close_hour = 13 if prev in HALF_DAYS else 16
    start = datetime.combine(prev, datetime.min.time(), ET) + timedelta(hours=close_hour)
    end = datetime.combine(nxt, datetime.min.time(), ET) + timedelta(hours=9, minutes=30)
    return start, end
