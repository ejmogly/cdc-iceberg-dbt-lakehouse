"""F 화면 8번 칸(앞으로의 범위)의 계산(docs/ai/F-screen.md 2절 8번 칸, 4절 「범위 칸」).

화면 서버가 화면 데이터 파일(data/generated/screen.duckdb)의 종가로 부르는 쪽입니다. 독립 정답
(eval/f_screen/range_expected.py, 원본을 직접 읽음)과 따로 짠 코드이고, 그 파일을 가져다 쓰지 않습니다.

- 대상은 현지 통화 가격(미국은 수정한 종가, 국내는 종가)의 일별 로그 수익률입니다. 환율과 분배금은 넣지 않아 원화 범위가 아닙니다.
- E03 기준선 「최근」: 기준일 t까지의 마지막 로그 수익률 20개로 s = sqrt(제곱의 합 / 20) + 1e-9(평균을 빼지 않음).
  앞으로 20거래일 로그 수익률의 80% 범위는 -1.2816 x s x sqrt(20) ~ +1.2816 x s x sqrt(20).
- 지난날 적중률: 예측 날짜를 t - 20, t - 40, ... 거래일로 거슬러 가며, 앞에 수익률 20개가 있는 창만 셉니다(겹치지 않음,
  마지막 창의 끝이 t). 그 창의 실제 20거래일 로그 수익률이 범위 안(경계 포함)이면 맞음. t 뒤의 값은 읽지 않습니다.
- 표시: n이 20보다 작으면 「표본 문구」, 아니면 적중률이 70 ~ 90% 밖일 때 「알림」, 안이면 「없음」.
- 화면은 숫자를 다시 반올림하지 않으므로, 보일 값(범위 %는 소수 둘째 자리, 적중률은 소수 첫째 자리)을 여기서 정합니다.
  0.5는 0에서 먼 쪽으로 올립니다(손익 %와 같은 규칙. 파이썬 round는 짝수 쪽으로 보내 81.25%가 81.2%가 됨).
- 알림이면 어느 쪽으로 벗어났는지(side)도 줍니다. 90%를 넘으면 범위가 지난날의 움직임보다 넓었고, 70%에 못 미치면 좁았습니다.
"""
import math
from decimal import ROUND_HALF_UP, Decimal

AHEAD = 20       # 앞으로 볼 거래일 수
PAST = 20        # 「최근」이 보는 일별 로그 수익률 수
Z = 1.2816       # 표준정규 90% 지점(80% 범위의 위 끝)
GUARD = 1e-9     # E03 코드가 로그를 보호하려 더한 값
ALERT = (0.70, 0.90)
SMALL_N = 20


def band(px, t):
    """가격 순번 t의 장 마감까지 아는 값(px[t - 20] ~ px[t])으로 낸 80% 범위(로그 수익률). 수익률 20개가 없으면 None."""
    if t < PAST:
        return None
    sq = 0.0
    for i in range(t - PAST + 1, t + 1):
        r = math.log(px[i] / px[i - 1])
        sq += r * r
    half = Z * (math.sqrt(sq / PAST) + GUARD) * math.sqrt(AHEAD)
    return -half, half


def past(px, t):
    """t에서 20거래일씩 거슬러 간 겹치지 않는 창. 오래된 순으로 (예측 순번, 아래, 위, 실제, 맞음)."""
    out = []
    p = t - AHEAD
    while p >= PAST:
        lo, hi = band(px, p)
        actual = math.log(px[p + AHEAD] / px[p])
        out.append((p, lo, hi, actual, lo <= actual <= hi))
        p -= AHEAD
    out.reverse()
    return out


def half_up(x, places):
    """0.5를 0에서 먼 쪽으로 올리는 반올림. x는 float나 Decimal이고, 그 값 그대로(2진수 값 그대로) 셉니다."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(x).quantize(q, rounding=ROUND_HALF_UP))


def to_pct(x):
    """로그 수익률을 가격 변화 %(소수 둘째 자리)로."""
    return half_up((math.exp(x) - 1) * 100, 2)


def view(days, px, t):
    """API가 돌려줄 8번 칸 값. days와 px는 같은 길이의 거래일 목록과 가격 목록, t는 기준일의 순번."""
    b = band(px, t)
    ws = past(px, t)
    n = len(ws)
    hits = sum(1 for w in ws if w[4])
    rate = hits / n if n else None
    if n < SMALL_N:
        display = "표본 문구"
    elif rate is None or not (ALERT[0] <= rate <= ALERT[1]):
        display = "알림"
    else:
        display = "없음"
    side = None
    if display == "알림" and rate is not None:
        side = "넓음" if rate > ALERT[1] else "좁음"
    return {
        "base_date": days[t],
        "ahead": AHEAD, "past": PAST, "z": Z,
        "ok": b is not None,
        "lo_log": b[0] if b else None, "hi_log": b[1] if b else None,
        "lo_pct": to_pct(b[0]) if b else None, "hi_pct": to_pct(b[1]) if b else None,
        "n": n, "hits": hits,
        "hit_rate_pct": half_up(Decimal(hits) * 100 / Decimal(n), 1) if n else None,
        "display": display, "side": side,
        "windows": [{"date": days[p], "lo_log": lo, "hi_log": hi, "actual_log": y, "hit": ok}
                    for p, lo, hi, y, ok in ws],
    }
