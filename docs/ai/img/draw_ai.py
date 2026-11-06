"""AI 트랙 구조 그림 생성기. 사용: python3 draw_ai.py <출력 폴더>
꾸밈은 docs/guide/img/draw_guide03.py와 같게 맞췄습니다."""
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
    "cdc": ("#f1f0ff", "#8b7cf6", "#5b21b6"),
    "kafka": ("#fff4e5", "#f0a14a", "#9a4d00"),
    "lake": ("#e8f7fb", "#36a3c9", "#0b5c78"),
    "ok": ("#ecfdf3", "#3fb27f", "#0f6b45"),
    "gray": ("#f6f8fa", "#afb8c1", "#57606a"),
    "warn": ("#fff1f0", "#e5534b", "#b42318"),
    "note": ("#fffbea", "#d4a72c", "#7a5d00"),
}


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.parts, self.title, self.n = w, h, [], title, 0

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=14, color=INK, weight=400, anchor="start", mono=False):
        fam = MONO if mono else FONT
        self.add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" font-weight="{weight}" '
                 f'fill="{color}" text-anchor="{anchor}">{escape(s)}</text>')

    def box(self, x, y, w, h, kind, title, sub=(), dashed=False, tsize=14, ssize=12):
        fill, stroke, ink = C[kind]
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="1.5"{dash}/>')
        total = tsize * 1.3 + len(sub) * ssize * 1.4
        cy = y + h / 2 - total / 2 + tsize * 0.9
        self.text(x + w / 2, cy, title, tsize, ink, 700, "middle")
        cy += tsize * 1.3 + 1
        for s in sub:
            self.text(x + w / 2, cy, s, ssize, INK2, 400, "middle")
            cy += ssize * 1.4

    def frame(self, x, y, w, h, label, color=LINE):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="none" '
                 f'stroke="{color}" stroke-width="1.4" stroke-dasharray="5 5"/>')
        self.text(x + 12, y - 8, label, 13, MUTED, 700)

    def arrow(self, points, color=MUTED, dashed=False, width=1.7):
        self.n += 1
        mid = f"m{self.n}"
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        pts = " ".join(f"{x},{y}" for x, y in points)
        self.add(f'<defs><marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                 f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/>'
                 f'</marker></defs>')
        self.add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}"{dash} '
                 f'marker-end="url(#{mid})"/>')

    def save(self, name):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
                f'width="{self.w}" height="{self.h}" role="img" aria-label="{escape(self.title)}">'
                f'<title>{escape(self.title)}</title><rect width="100%" height="100%" fill="{BG}"/>')
        (OUT / name).write_text(head + "\n".join(self.parts) + "</svg>\n", encoding="utf-8")


s = Svg(1320, 620, "AI 트랙 구조: 데이터 파이프라인 위에 도구, 에이전트, 화면, 평가 실행기")
s.text(30, 30, "데이터 파이프라인이 만든 Gold 위에 도구와 에이전트를 얹고, 평가 실행기가 모든 단계를 잽니다", 17, INK, 700)

# 1열: 원천
X1, W1 = 30, 190
s.frame(X1 - 10, 80, W1 + 20, 330, "원천 DB")
s.box(X1, 92, W1, 86, "src", "주문, 체결, 잔고", ("합성 데이터",))
s.box(X1, 196, W1, 86, "src", "시세, 환율", ("공공 데이터, 하루 한 번",))
s.box(X1, 300, W1, 96, "src", "관심도, 뉴스", ("GDELT, 위키백과", "종목 뉴스 제목과 감성"))

# 2열: 파이프라인
X2, W2 = 270, 170
s.frame(X2 - 10, 80, W2 + 20, 330, "데이터 파이프라인")
ys = [96, 168, 240, 312]
names = [("CDC, Kafka", "kafka"), ("Bronze", "lake"), ("Silver", "lake"), ("Gold", "ok")]
for (nm, kd), y in zip(names, ys):
    s.box(X2, y, W2, 56, kd, nm)
for a, b in zip(ys, ys[1:]):
    s.arrow([(X2 + W2 / 2, a + 56), (X2 + W2 / 2, b - 2)])
for y, ty in ((135, 112), (239, 124), (348, 136)):
    s.arrow([(X1 + W1, y), (X1 + W1 + 22, y), (X2 - 2, ty)], MUTED)

# 3열: 도구
X3, W3 = 490, 200
s.frame(X3 - 10, 80, W3 + 20, 330, "도구")
tools = [("손익 계산기", "A, B. 결정적 계산", "ok"), ("SQL 조회", "Gold 읽기만", "ok"),
         ("뉴스 검색", "C. 날짜와 종목 거름 + 의미 검색", "cdc"), ("비슷했던 때 찾기", "D. 유사 국면과 그 뒤 범위", "cdc")]
for i, (t, sub, kd) in enumerate(tools):
    s.box(X3, 92 + i * 78, W3, 64, kd, t, (sub,), ssize=11)
s.arrow([(X2 + W2, 340), (X3 - 2, 340)], C["ok"][1])
s.text((X2 + W2 + X3) / 2, 332, "읽기", 11, MUTED, 400, "middle")

# 4열: 에이전트
X4, W4 = 740, 230
s.frame(X4 - 10, 80, W4 + 20, 330, "에이전트 (E)")
steps = [("계획", "질문을 도구 호출로 나눔"), ("실행", "도구를 부르고 결과를 모음"),
         ("검증", "숫자를 다시 계산, 인과 표현 검사")]
for i, (t, sub) in enumerate(steps):
    s.box(X4, 96 + i * 100, W4, 72, "warn" if t == "검증" else "gray", t, (sub,), ssize=11)
for i in range(2):
    s.arrow([(X4 + W4 / 2, 168 + i * 100), (X4 + W4 / 2, 194 + i * 100)])
s.arrow([(X4 + W4 - 20, 296), (X4 + W4 + 4, 296), (X4 + W4 + 4, 132), (X4 + W4 - 2, 132)], C["warn"][1], dashed=True)
s.text(X4 + W4 + 8, 220, "틀리면", 11, C["warn"][2], 400)
s.text(X4 + W4 + 8, 236, "다시", 11, C["warn"][2], 400)
s.arrow([(X4 - 2, 230), (X3 + W3 + 2, 230)], MUTED)
s.arrow([(X3 + W3 + 2, 250), (X4 - 2, 250)], MUTED)

# 5열: 화면
X5, W5 = 1040, 250
s.frame(X5 - 10, 80, W5 + 20, 330, "화면 (F)")
s.box(X5, 96, W5, 84, "src", "대시보드", ("종목, 매수일, 매도일, 수량", "손익, 환율 효과, 기준선 대비"), ssize=11)
s.box(X5, 198, W5, 84, "src", "대화창", ("질문과 답, 근거 인용", "범위로만 말하는 전망"), ssize=11)
s.box(X5, 300, W5, 96, "gray", "미니어처 도시 (선택)", ("종목마다 건물, 높이는 평가금액", "색은 수익률, 날짜 슬라이더"), dashed=True, ssize=11)
s.arrow([(X4 + W4 + 30, 240), (X5 - 2, 240)], MUTED)
s.arrow([(X3 + W3 / 2 + 40, 92), (X3 + W3 / 2 + 40, 54), (X5 + W5 / 2, 54), (X5 + W5 / 2, 94)], C["ok"][1], dashed=True)
s.text((X3 + X5 + W5) / 2 + 20, 49, "대시보드는 에이전트 없이 계산기를 바로 씀", 11, MUTED, 400, "middle")

# 아래: 평가 실행기
s.box(30, 450, 1260, 92, "note", "평가 실행기",
      ("골든셋(손익 사례 30, 질문 60, 큰 움직임 날 40) → 시스템 → 채점(규칙, 판정 모델, 사람 표본) → 품질, p50과 p95 지연, 비용",
       "기능마다 평가 기준을 개발 전에 확정. 손잡이(모델 크기, 검색 개수, 캐시, 미리 계산)를 바꿔 파레토 그림"), ssize=12)
for x in (X3 + W3 / 2, X4 + W4 / 2, X5 + W5 / 2, X2 + W2 / 2):
    s.arrow([(x, 448), (x, 414)], C["note"][1], dashed=True)
s.text(30, 590, "투자 권유가 아닙니다. 데이터 사이의 관계는 \"같이 움직였다\"(상관)까지만 말합니다.", 13, INK2)
s.save("ai-architecture.svg")
print("그림 1개:", OUT)
