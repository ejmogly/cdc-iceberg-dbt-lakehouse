"""원천 설계 그림 생성기. 사용: python3 docs/design/img/draw_design.py docs/design/img
꾸밈은 학습 가이드의 그림 스크립트와 같게 맞췄습니다."""
import sys
from pathlib import Path
from html import escape

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

FONT = "'Apple SD Gothic Neo','Pretendard','Noto Sans KR',sans-serif"
MONO = "'SF Mono','Menlo',monospace"
INK, INK2, MUTED, LINE, BG = "#1f2328", "#424a53", "#6e7781", "#d0d7de", "#ffffff"
C = {  # 면, 테두리, 글자
    "src": ("#eef4ff", "#5b8def", "#1d4ed8"),
    "kafka": ("#fff4e5", "#f0a14a", "#9a4d00"),
    "lake": ("#e8f7fb", "#36a3c9", "#0b5c78"),
    "gray": ("#f6f8fa", "#afb8c1", "#57606a"),
    "warn": ("#fff1f0", "#e5534b", "#b42318"),
    "ok": ("#ecfdf3", "#3fb27f", "#0f6b45"),
    "note": ("#fffbea", "#d4a72c", "#7a5d00"),
}


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.parts, self.title = w, h, [], title
        self.n = 0

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=14, color=INK, weight=400, anchor="start", mono=False):
        fam = MONO if mono else FONT
        self.add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" font-weight="{weight}" '
                 f'fill="{color}" text-anchor="{anchor}">{escape(s)}</text>')

    def rect(self, x, y, w, h, kind="gray", r=10, dashed=False, sw=1.5):
        fill, stroke, _ = C[kind]
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{dash}/>')

    def frame(self, x, y, w, h, color, r=10):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="none" '
                 f'stroke="{color}" stroke-width="1.6" stroke-dasharray="6 5"/>')

    def arrow(self, points, color=MUTED, dashed=False, width=1.8):
        self.n += 1
        mid = f"m{self.n}"
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        pts = " ".join(f"{x},{y}" for x, y in points)
        self.add(f'<defs><marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                 f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/>'
                 f'</marker></defs>')
        self.add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}"{dash} '
                 f'marker-end="url(#{mid})"/>')

    def line(self, x1, y1, x2, y2, color=LINE, dashed=False, width=1.2):
        dash = ' stroke-dasharray="4 4"' if dashed else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                 f'stroke-width="{width}"{dash}/>')

    def chip(self, x, y, w, h, s, kind="gray", size=12):
        fill, stroke, ink = C[kind]
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="1.3"/>')
        self.text(x + 9, y + h / 2 + size * 0.36, s, size, ink, 600, "start", True)

    def save(self, name):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
                f'width="{self.w}" height="{self.h}" role="img" aria-label="{escape(self.title)}">'
                f'<title>{escape(self.title)}</title><rect width="100%" height="100%" fill="{BG}"/>')
        (OUT / name).write_text(head + "\n".join(self.parts) + "</svg>\n", encoding="utf-8")


# 그림 1. 원천 테이블 네 개 ------------------------------------------------------
TABLES = [
    ("orders", "src", "한 행: 주문 하나", "삭제 없음, 취소는 status 수정", "합성", [
        ("order_id", "bigint", "PK"), ("account_id", "text", ""), ("symbol", "text", ""),
        ("side", "text", ""), ("order_type", "text", ""), ("order_qty", "int", ""),
        ("limit_price", "numeric(18,4)", ""), ("filled_qty", "int", ""), ("status", "text", ""),
        ("ordered_at", "timestamptz", ""), ("updated_at", "timestamptz", ""),
    ]),
    ("executions", "kafka", "한 행: 체결 한 건", "철회만 물리 삭제(드묾)", "합성", [
        ("execution_id", "bigint", "PK"), ("order_id", "bigint", "FK"),
        ("exec_qty", "int", ""), ("exec_price", "numeric(18,4)", ""),
        ("fx_rate", "numeric(12,4)", ""),
        ("executed_at", "timestamptz", ""), ("updated_at", "timestamptz", ""),
    ]),
    ("holdings", "lake", "한 행: 계좌의 한 종목 보유", "전량 매도하면 행 삭제", "합성", [
        ("account_id", "text", "PK"), ("symbol", "text", "PK"), ("qty", "int", ""),
        ("avg_price", "numeric(18,4)", ""), ("updated_at", "timestamptz", ""),
    ]),
    ("daily_prices", "ok", "한 행: 한 종목의 한 거래일 시세", "삭제 없음, 정정은 수정", "실제(공공)", [
        ("symbol", "text", "PK"), ("trade_date", "date", "PK"),
        ("open_price", "numeric(18,4)", ""), ("high_price", "numeric(18,4)", ""),
        ("low_price", "numeric(18,4)", ""), ("close_price", "numeric(18,4)", ""),
        ("volume", "bigint", ""), ("provider", "text", ""), ("loaded_at", "timestamptz", ""),
    ]),
]
BW, HEAD, ROW, X0, GAP, Y0 = 270, 38, 24, 40, 76, 70
s = Svg(X0 * 2 + BW * 4 + GAP * 3, 530, "증권 앱 원천 테이블 네 개와 관계")
s.text(X0, 38, "원천 테이블 네 개 (source 스키마)", 18, INK, 700)
rowy = {}
for i, (name, kind, meaning, policy, data, cols) in enumerate(TABLES):
    x = X0 + i * (BW + GAP)
    h = HEAD + ROW * len(cols) + 10
    s.rect(x, Y0, BW, h, "gray", 10)
    s.add(f'<rect x="{x}" y="{Y0}" width="{BW}" height="{HEAD}" rx="10" fill="{C[kind][0]}" '
          f'stroke="{C[kind][1]}" stroke-width="1.5"/>')
    s.text(x + 14, Y0 + 25, f"source.{name}", 15, C[kind][2], 700, "start", True)
    for j, (col, typ, key) in enumerate(cols):
        y = Y0 + HEAD + 18 + j * ROW
        rowy[(name, col)] = y - 4
        s.text(x + 14, y, col, 13, INK, 600 if key else 400, "start", True)
        s.text(x + 150, y, typ, 12, MUTED, 400, "start", True)
        if key:
            s.chip(x + BW - 46, y - 14, 36, 19, key, "note" if key == "PK" else "warn", 11)
    s.text(x, Y0 + h + 26, meaning, 14, INK, 600)
    s.text(x, Y0 + h + 48, policy, 13, INK2)
    real = data.startswith("실제")
    s.chip(x, Y0 + h + 60, 160 if real else 104, 24, f"데이터: {data}", "ok" if real else "gray", 12)
    if name == "executions":  # 철회 이벤트에 order_id가 있어야 해서 이 테이블만 FULL
        s.chip(x, Y0 + h + 92, 196, 24, "REPLICA IDENTITY FULL", "note", 12)

# executions.order_id → orders.order_id
ex = X0 + BW + GAP
y1, y2 = rowy[("executions", "order_id")], rowy[("orders", "order_id")]
s.arrow([(ex, y1), (ex - GAP / 2, y1), (ex - GAP / 2, y2), (ex - GAP + 8, y2)], C["warn"][1])
s.text(ex - GAP / 2, y1 + 18, "FK", 12, C["warn"][2], 700, "middle")

# 체결이 잔고를 바꿈(FK 아님, 같은 트랜잭션)
hx = X0 + 2 * (BW + GAP)
ymid = Y0 + HEAD + ROW * 2
s.arrow([(hx - GAP, ymid), (hx - 8, ymid)], MUTED, dashed=True)
s.text(hx - GAP / 2, ymid - 10, "같은", 11, MUTED, 400, "middle")
s.text(hx - GAP / 2, ymid + 20, "커밋", 11, MUTED, 400, "middle")

# 잔고와 시세: FK 없이 Gold에서 symbol로 조인
px = X0 + 3 * (BW + GAP)
ys = rowy[("holdings", "symbol")]
s.line(px - GAP, ys, px, ys, MUTED, dashed=True, width=1.6)
s.text(px - GAP / 2, ys - 10, "symbol", 11, MUTED, 400, "middle", True)
s.text(px - GAP / 2, ys + 20, "Gold 조인", 11, MUTED, 400, "middle")

s.text(X0, 512, "FK는 executions → orders 하나뿐입니다. 잔고는 체결과 같은 트랜잭션에서 앱이 고치고, 시세는 다음 날 들어오므로 Gold에서 조인합니다.", 13, INK2)
s.save("design-erd.svg")


# 그림 2. 시나리오: 단계마다 생기는 변경 이벤트 --------------------------------------
STEPS = [
    ("1 매수 주문", "지정가 10주 70,000"),
    ("2 가격 정정", "70,000 → 69,500"),
    ("3 4주 체결", "한 트랜잭션"),
    ("4 잔량 취소", "남은 6주"),
    ("5 매도 주문", "시장가 4주"),
    ("6 전량 체결", "한 트랜잭션"),
]
LANES = [("orders", "src"), ("executions", "kafka"), ("holdings", "lake")]
CELLS = {  # (단계, 줄): [(글, 종류)]
    (0, 0): [("c 1001 ACCEPTED", "src")],
    (1, 0): [("u 1001 69,500", "src")],
    (2, 0): [("u 1001 PARTIAL", "src")],
    (2, 1): [("c 5001 4@69,500", "kafka")],
    (2, 2): [("c A-0001 005930", "lake")],
    (3, 0): [("u 1001 CANCELLED", "src")],
    (4, 0): [("c 1002 ACCEPTED", "src")],
    (5, 0): [("u 1002 FILLED", "src")],
    (5, 1): [("c 5002 4@70,100", "kafka")],
    (5, 2): [("d A-0001 005930", "warn"), ("tombstone", "gray")],
}
ROWS_AFTER = ["1, 0, 0", "1, 0, 0", "1, 1, 1", "1, 1, 1", "2, 1, 1", "2, 2, 0"]
BRONZE = ["6줄: c u u u c u", "2줄: c c", "2줄: c d"]

LX, CW, X1, TOP, LH = 30, 150, 150, 120, 92
BX = X1 + CW * len(STEPS) + 20
W = BX + 180 + 30
s = Svg(W, TOP + LH * 3 + 130, "증권 앱 시나리오에서 테이블마다 생기는 변경 이벤트")
s.text(LX, 36, "시나리오: 계좌 A-0001이 삼성전자(005930)를 사고, 정정하고, 취소하고, 전부 판다", 18, INK, 700)
s.text(LX, 62, "칸의 글자는 Kafka에 들어갈 변경 이벤트(op와 키)입니다. 줄은 원천 테이블, 열은 업무 단계입니다.", 13, INK2)

for i, (t, sub) in enumerate(STEPS):
    cx = X1 + i * CW + CW / 2
    s.text(cx, TOP - 26, t, 14, INK, 700, "middle")
    s.text(cx, TOP - 8, sub, 12, MUTED, 400, "middle")
s.text(BX + 90, TOP - 26, "Bronze에 남는 것", 14, INK, 700, "middle")
s.text(BX + 90, TOP - 8, "tombstone은 뺌", 12, MUTED, 400, "middle")

for r, (name, kind) in enumerate(LANES):
    y = TOP + r * LH
    s.line(LX, y, W - 30, y, LINE)
    s.text(LX, y + LH / 2 + 5, name, 14, C[kind][2], 700, "start", True)
    for c in range(len(STEPS)):
        items = CELLS.get((c, r), [])
        for k, (txt, kk) in enumerate(items):
            cy = y + (LH - len(items) * 30) / 2 + k * 32
            s.chip(X1 + c * CW + 6, cy, CW - 12, 26, txt, kk, 11)
        if not items:
            s.text(X1 + c * CW + CW / 2, y + LH / 2 + 5, "없음", 12, LINE, 400, "middle")
    s.chip(BX + 6, y + LH / 2 - 13, 168, 26, BRONZE[r], "ok", 12)
s.line(LX, TOP + LH * 3, W - 30, TOP + LH * 3, LINE)

for c in (2, 5):  # 한 트랜잭션: 세 테이블이 같은 커밋
    s.frame(X1 + c * CW + 2, TOP + 6, CW - 4, LH * 3 - 12, C["note"][1])

yb = TOP + LH * 3 + 34
s.text(LX, yb, "원천 행 수", 13, INK, 700)
s.text(LX, yb + 18, "(주문, 체결, 잔고)", 11, MUTED)
for c, v in enumerate(ROWS_AFTER):
    s.text(X1 + c * CW + CW / 2, yb + 6, v, 14, INK, 600, "middle", True)
s.text(LX, yb + 62, "노란 점선 칸은 한 트랜잭션입니다. Kafka에서는 토픽 세 개로 갈라지므로, Bronze에는 체결만 먼저 보이는 순간이 있을 수 있습니다.", 13, INK2)
s.save("design-scenario.svg")


# 그림 3. 파이프라인: 원천에서 Gold까지 단계와 역할 -----------------------------------
STAGES = [  # (이름, 종류, 역할 두 줄, 상태, 구현됨, 고민 두 줄)
    ("PostgreSQL", "src", ("source 스키마 테이블 넷", "주문, 체결, 잔고, 시세"),
     "구현함", True, ("정정을 새 주문 번호 대신", "한 행 수정으로 줄임")),
    ("Debezium", "kafka", ("WAL의 변경을 읽어", "c, u, d 이벤트로"),
     "구현함", True, ("slot이 밀리면 원천에", "WAL이 쌓임")),
    ("Kafka", "kafka", ("테이블마다 토픽 하나", "src.source.*"),
     "구현함", True, ("한 트랜잭션이", "토픽 셋으로 갈라짐")),
    ("Flink", "lake", ("토픽을 계속 읽어", "원문 그대로 덧붙임"),
     "4회차", False, ("checkpoint 간격과", "작은 파일 수")),
    ("Iceberg Bronze", "lake", ("모든 이력 보존", "c, u, d(tombstone 뺌)"),
     "4회차", False, ("빈 snapshot이 쌓임", "(7회차에 정리)")),
    ("Silver", "lake", ("키마다 최신 하나", "MERGE, is_deleted"),
     "4회차", False, ("최신은 LSN 먼저,", "같으면 offset")),
    ("Gold (dbt)", "ok", ("체결 대금, 잔고 대사", "평가금액"),
     "5회차", False, ("정정된 체결이 바꾸는", "지난 날 지표(6회차)")),
]
SX, SW, SG, SY, SH = 30, 168, 26, 158, 116
W = SX * 2 + SW * len(STAGES) + SG * (len(STAGES) - 1)
s = Svg(W, 540, "증권 앱 파이프라인: 원천 DB에서 Gold 지표까지")
s.text(SX, 36, "파이프라인: 원천 DB에서 Gold 지표까지", 18, INK, 700)
s.text(SX, 60, "위 상자는 단계마다 맡는 일, 아래 점선 상자는 아직 고민되는 점입니다. 초록 칩은 이 저장소에 이미 구현한 것, 회색 칩은 그 회차에 만들 것입니다.", 13, INK2)

# 시세 적재기: 하루 한 번 원천 daily_prices에 넣음
s.rect(SX, 80, 330, 40, "note", 8)
s.text(SX + 14, 105, "시세 적재기: 공공 데이터를 하루 한 번 받아 넣음", 13, C["note"][2], 600)
s.arrow([(SX + 60, 120), (SX + 60, SY - 4)], C["note"][1])

for i, (name, kind, role, state, done, worry) in enumerate(STAGES):
    x = SX + i * (SW + SG)
    s.rect(x, SY, SW, SH, kind, 10)
    s.text(x + 12, SY + 28, name, 15, C[kind][2], 700)
    s.text(x + 12, SY + 54, role[0], 13, INK)
    s.text(x + 12, SY + 74, role[1], 13, INK)
    s.chip(x + 12, SY + 84, 70, 22, state, "ok" if done else "gray", 12)
    if i < len(STAGES) - 1:
        s.arrow([(x + SW, SY + SH / 2), (x + SW + SG - 4, SY + SH / 2)], MUTED)
    s.frame(x, SY + SH + 26, SW, 62, C["gray"][1], 8)
    s.text(x + 12, SY + SH + 50, worry[0], 12, INK2)
    s.text(x + 12, SY + SH + 70, worry[1], 12, INK2)


def bracket(x1, x2, y, label):
    s.line(x1, y - 8, x1, y, MUTED, width=1.4)
    s.line(x2, y - 8, x2, y, MUTED, width=1.4)
    s.line(x1, y, x2, y, MUTED, width=1.4)
    s.text(x1, y + 22, label, 13, INK, 600)


bx = lambda i: SX + i * (SW + SG)  # noqa: E731
bracket(bx(0), bx(4) + SW, 396, "원천 변경 → Bronze 조회: 1분 안 (잠정)")
bracket(bx(0), bx(6) + SW, 452, "원천 변경 → Gold 지표: 장중 현황 5분 안, 일별 확정 지표는 시장 마감 뒤 시세가 들어온 때부터 30분 안 (잠정)")
s.text(SX, 516, "Spark SQL이 Silver를, dbt가 Gold를 만듭니다. 원천부터 Kafka까지는 3회차에 이 저장소에서 만들어 시나리오로 확인했고, Flink부터는 4회차에 붙입니다.", 13, INK2)
s.save("design-pipeline.svg")
print("그림 3개:", OUT)
