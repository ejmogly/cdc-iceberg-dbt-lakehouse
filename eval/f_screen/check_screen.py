"""F 화면(screen/)이 계산 서버의 값을 그대로 보이는지 대조합니다(docs/ai/F-screen.md 4절).

  - 값 대조: 실제 데이터 사례 20개(cases_real.json, 실제 12와 경고 8)를 화면 서버에 넣고, 헤드리스 Chrome으로 화면을 그린 뒤
    DOM의 글자를 API 값과 비교합니다. 칸마다 이 파일의 표시 규칙(자릿점, 부호, 단위, 모자란 소수 자리만 0)으로 기대 글자를
    따로 만들어 **글자째 같은지** 보고(허용 오차 없음), 색 클래스(이익 up, 손실 down)가 값의 부호와 맞는지, 「판정 전」 띠가
    있어야 할 때만 있는지도 봅니다. 화면 코드(screen/*.js)의 표시 함수를 쓰지 않습니다. 체결가와 환율은 check_api.py처럼
    정답 파일의 inputs(입력 값)를 넣고, 매매 줄은 저장 파일에서 다시 열린 입력 칸 값도 봅니다.
  - 대비: screen/style.css의 밝은 화면과 어두운 화면 색 토큰으로 글자와 바탕의 대비를 셉니다(4.5:1 이상). 어두운 화면의 두
    블록(시스템 설정, data-theme)이 같은지도 봅니다.
  - 자체 시험(--selftest): 국내 사례 하나와 미국 사례 하나의 DOM을 일부러 틀리게 바꿔(1원, 색, 단위, 부호, 판정 전 띠 등)
    대조기가 모두 잡는지 봅니다.

사용: .venv/bin/python eval/f_screen/check_screen.py [--only R01,W03] [--selftest]
결과: data/generated/f_screen/screen_check.json(git 제외, 실제 값이 들어 있음)

화면은 저장된 매매를 열 때 그리므로, 사례마다 git 제외 폴더의 임시 저장 파일에 매매를 쓰고 서버를 그 파일로 띄웁니다.
입력 흐름(빠른 고침, 저장 실패, 틀린 기준일, 종목 바꾸기)은 check_flows.py가 봅니다. 375px 폭과 키보드 조작은 사람이
브라우저에서 봅니다(설계서 4절 「화면 품질」).
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai import screen_data  # noqa: E402
from ai.screen_server import SAFE, serve, stop  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CASES = ROOT / "eval" / "f_screen" / "cases_real.json"
EXPECTED = SAFE / "f_screen" / "expected_real.json"
TMP = SAFE / "f_screen" / "check_screen_trades.json"
OUT = SAFE / "f_screen" / "screen_check.json"
CSS = ROOT / "screen" / "style.css"


# ---------- DOM 읽기 ----------

class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.kids, self.parts = tag, dict(attrs), parent, [], []

    def text(self):
        out = []
        for p in self.parts:
            out.append(p.text() if isinstance(p, Node) else p)
        return "".join(out)

    def walk(self):
        yield self
        for k in self.kids:
            yield from k.walk()

    def find(self, tag=None, cls=None):
        return [n for n in self.walk() if n is not self and (tag is None or n.tag == tag)
                and (cls is None or cls in (n.attrs.get("class") or "").split())]


VOID = {"meta", "link", "input", "br", "img", "hr", "source", "path", "circle", "line", "rect"}


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {}, None)
        self.cur = self.root
        self.ids = {}

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.kids.append(n)
        self.cur.parts.append(n)
        if "id" in n.attrs:
            self.ids[n.attrs["id"]] = n
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.cur = self.cur.parent

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.parts.append(data)


def dom(html):
    t = Tree()
    t.feed(html)
    return t


# ---------- 기대 글자(설계서 7절 4단계 정한 것 1번: 자릿점, 부호, 단위, 모자란 소수 자리의 0만 붙임) ----------

MINUS = "−"


def fnum(v, places=0):
    """반올림하지 않고 자릿점을 넣습니다. JSON의 21.0은 브라우저에서 21이라 정수로 봅니다."""
    if v is None:
        return "–"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = format(Decimal(str(v)), "f")
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    i, _, f = s.partition(".")
    f = f.ljust(places, "0")
    return (MINUS if neg and Decimal(s) != 0 else "") + f"{int(i):,}" + ("." + f if f else "")


def plus(v, signed):
    return "+" if signed and v > 0 else ""


def fkrw(v, signed=False):
    return "–" if v is None else plus(v, signed) + fnum(v) + "원"


def flocal(v, cur, signed=False):
    if v is None:
        return "–"
    if cur == "KRW":
        return fkrw(v, signed)
    return (MINUS if v < 0 else plus(v, signed)) + "$" + fnum(abs(v), 2)


def fprice(v, cur):
    return "–" if v is None else fnum(v) if cur == "KRW" else fnum(v, 2)


def fpct(v, signed=True):
    return "–" if v is None else plus(v, signed) + fnum(v, 2) + "%"


def fbig(v, signed=False):
    """큰 숫자 칸과 결론 문장의 줄인 금액(11/18 화면 개편). 100만 이상 만, 1억 이상 억(100억 이상 정수), 1조 이상 조. 그 아래는 원 단위 그대로."""
    from decimal import ROUND_HALF_UP, Decimal
    if v is None:
        return "–"
    a = Decimal(abs(v))
    sign = MINUS if v < 0 else ("+" if signed and v > 0 else "")
    one = lambda x: (lambda q: str(int(q)) if q == q.to_integral() else str(q))(x.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    whole = lambda x: f"{int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP)):,}"
    if a >= Decimal(10) ** 12:
        return sign + one(a / Decimal(10) ** 12) + "조원"
    if a >= Decimal(10) ** 8:
        q = a / Decimal(10) ** 8
        return sign + (whole(q) if q >= 100 else one(q)) + "억원"
    if a >= Decimal(10) ** 6:
        return sign + whole(a / Decimal(10) ** 4) + "만원"
    return fkrw(v, signed)


def shortened(v):
    return v is not None and abs(v) >= 10 ** 6


def ftone(v):
    return "na" if v is None else "up" if v > 0 else "down" if v < 0 else "zero"


# ---------- 사례 하나 ----------

def chrome_dom(url, width=1280):
    with tempfile.TemporaryDirectory() as prof:
        r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                            f"--user-data-dir={prof}", f"--window-size={width},2400", "--virtual-time-budget=20000",
                            "--dump-dom", url], capture_output=True, text=True, timeout=90)
    return r.stdout


def post(port, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/pnl", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


sys.path.insert(0, str(ROOT / "eval" / "e04_agent"))
from score import violations  # noqa: E402  E04 채점기의 금지 표현 규칙

# 개발 검사 사례(평가셋이 아님): 평가셋 20개에 없는 화면 갈래. 정답 파일 없이 API 값과 화면을 대조합니다(2026-11-17 다듬기 독립 검토 낮음 7번)
EXTRA = [
    {"id": "X01", "title": "매도만 넣어 장부에 매수가 없음", "symbol": "QQQ", "asof": "2026-11-12",
     "trades": [{"d": "2025-03-03", "side": "SELL", "q": 10}]},
    {"id": "X02", "title": "SPY와 차이가 0원", "symbol": "QQQ", "asof": "2026-11-12",
     "trades": [{"d": "2026-11-11", "side": "BUY", "q": 1}]},
    {"id": "X03", "title": "모든 줄이 계산 앞에서 빠져 요약이 없음(소수 수량)", "symbol": "QQQ", "asof": "2026-11-12",
     "trades": [{"d": "2025-03-03", "side": "BUY", "q": "1.5"}]},
    {"id": "X04", "title": "매도가 초과 매도로 빠짐", "symbol": "QQQ", "asof": "2026-11-12",
     "trades": [{"d": "2025-03-03", "side": "BUY", "q": 10}, {"d": "2025-08-01", "side": "SELL", "q": 20}]},
    {"id": "X05", "title": "매도 없음, 줄을 빼지 않는 경고(환율 직전 값)", "symbol": "QQQ", "asof": "2026-11-12",
     "trades": [{"d": "2025-03-03", "side": "BUY", "q": 10}]},
]

# 개발 검사 사례의 자체 시험 변형
EXTRA_MUT = {
    "X01": {"매수 없음 문장을 숨김": lambda h: h.replace('<p id="verdict" class="verdict">', '<p id="verdict" class="verdict" hidden="">', 1)},
    "X02": {"차이 0을 많음으로": lambda h: h.replace("으로 이 매매와 같아요.", "이라, 이 매매가 0원 더 많아요.", 1)},
    "X04": {"빠진 매도 안내를 매도 없음으로": lambda h: h.replace(
                "계산에 들어간 매도가 없어요. 빠진 매도 줄은 내 매매 탭의 경고를 봐 주세요.", "아직 판 적이 없어요.", 1),
            "매도 표 보임": lambda h: h.replace('<div id="sells-box" class="tablebox" hidden="">', '<div id="sells-box" class="tablebox">', 1)},
    "X05": {"빠지지 않았는데 빠진 매도 안내": lambda h: h.replace(
                ">아직 판 적이 없어요.<", ">계산에 들어간 매도가 없어요. 빠진 매도 줄은 내 매매 탭의 경고를 봐 주세요.<", 1)},
}


# 증권사와 지금 잔고(11/19 중간 판 의견, ai/holdings.py): 화면 글자로 넣는 개발 사례. 평가셋과 다른 갈래라 따로 대조합니다
ACCOUNT_CASES = [
    {"id": "A01", "title": "국내 종목을 두 증권사에서", "symbol": "005930", "asof": "2026-11-12", "rows": [
        {"date": "2025-03-04", "side": "BUY", "qty": "10", "broker": "TOSS"},
        {"date": "2025-06-02", "side": "BUY", "qty": "5", "broker": "KAKAOPAY"},
        {"date": "2025-09-01", "side": "SELL", "qty": "3", "broker": "KAKAOPAY"}]},
    {"id": "A02", "title": "미국 종목 지금 잔고(데이터 기간 전부터 보유)", "symbol": "QQQ", "asof": "2026-11-12", "rows": [
        {"date": "2023-05-01", "side": "BUY", "qty": "50", "price": "300", "fx_rate": "1300", "broker": "KIS"},
        {"date": "2025-03-03", "side": "BUY", "qty": "4", "broker": "KIS"},
        {"date": "2025-08-01", "side": "SELL", "qty": "6", "broker": "KIS"},
        {"side": "HOLD", "qty": "48", "price": "420", "fx_rate": "1350", "broker": "KIS"}]},
]
BROKER_NAMES = {"": "증권사 안 고름", "TOSS": "토스증권", "KAKAOPAY": "카카오페이증권", "KIS": "한국투자증권"}

ACCOUNT_MUT = {
    "A01": {"증권사별 수익률 바꿈": lambda h: re.sub(r'(<div id="acct-list".*?<span class="value[^"]*">)[^<]*',
                                             lambda x: x.group(1) + "+1.00%", h, count=1, flags=re.S),
            "매도에 증권사 이름 없음": lambda h: h.replace(" 카카오페이증권</td>", "</td>", 1)},
    "A02": {"SPY와 차이를 숫자로": lambda h: _span(h, "k-bench", lambda t, x: (t, "+1만원")),
            "지금 잔고 줄 평단 칸 바꿈": lambda h: re.sub(r'(<span class="avg"[^>]*>)첫날 ', lambda x: x.group(1) + "처음 ", h, count=1),
            "SPY 범례 보임": lambda h: re.sub(r'<span id="lg-bench" hidden="">', '<span id="lg-bench">', h, count=1)},
}


def check_account_case(case, port, html=None):
    """증권사와 지금 잔고 사례: 증권사별 카드, 내 보유 줄, SPY 칸, 매도 표, 매매 줄, 계산 안내, 그림을 API 값과 대조."""
    api = post(port, {"symbol": case["symbol"], "asof": case["asof"], "trades": case["rows"]})
    if html is None:
        write_saved(case, case["rows"])
        html = chrome_dom(f"http://127.0.0.1:{port}/")
    ids = dom(html).ids
    bad, cur = [], api["currency"]
    s = api["summary"]

    def text(i):
        return ids[i].text().strip() if i in ids else None

    def eq(where, got, want):
        if got != want:
            bad.append(f"{where}: 화면 {got!r}, 기대 {want!r}")

    state = text("state") or ""
    if not re.match(r"계산 \d", state) or "hidden" in ids["result"].attrs:
        return api, [f"화면이 계산을 마치지 못함: {state!r}"]
    # 증권사별 카드: 줄마다 이름, 큰 숫자(남은 주식 수익률), 남은 수량
    eq("증권사별 카드 보임", "hidden" in ids["acct-box"].attrs, False)
    items = [n for n in ids["acct-list"].find(cls="li")]
    eq("증권사 줄 수", len(items), len(api["accounts"]))
    for n, a in zip(items, api["accounts"]):
        eq(f"{a['broker_name']} 이름", n.find("b")[0].text(), BROKER_NAMES[a["broker"]])
        v = n.find(cls="value")[0].text()
        eq(f"{a['broker_name']} 남은 주식 수익률", v, fpct(a["holding_return_pct"]) if a["holding_return_pct"] is not None
           else ("모두 팖" if a["summary"] and a["summary"]["qty_left"] == 0 else "–"))
        sub = n.find(cls="sub")[0].text()
        if a["summary"] and f"남은 {fnum(a['summary']['qty_left'])}주" not in sub:
            bad.append(f"{a['broker_name']} 남은 수량 없음: {sub!r}")
    # 원화 손익 보조(실현과 평가는 증권사별 합)
    if f"실현 {fkrw(s['realized_krw'], True)}, 평가 {fkrw(s['unrealized_krw'], True)}" not in (text("k-krw-s") or ""):
        bad.append(f"원화 손익 보조: {text('k-krw-s')!r}")
    hold = {}
    for sp in ids["holding-line"].find("span"):
        b = sp.find("b")
        if b:
            hold[sp.text()[: -len(b[0].text())].strip()] = b[0].text()
    if api["multi"]:
        eq("평균 단가(여러 증권사)", hold.get("평균 단가"), "증권사마다 달라요(아래 증권사별)")
        eq("남은 주식 수익률 줄 없음(여러 증권사)", "남은 주식 수익률(증권사 앱의 수익률)" in hold, False)
        for tr, sl in zip([tr for tr in ids["sells"].find("tr")], api["sells"]):
            eq(f"매도 {sl['date']} 날짜와 증권사", tr.find("td")[0].text(), f"{sl['date']} {BROKER_NAMES[sl['broker']]}")
    if api["opening"]:
        eq("SPY와 차이(시작 보유)", text("k-bench"), "세지 않음")
        eq("연 환산(시작 보유)", text("k-irr"), "세지 않음")
        eq("결론 문장 끝(시작 보유)", (text("verdict") or "").endswith("데이터 첫날 전부터 가진 주식이 있어 SPY와는 견주지 않아요."), True)
        eq("보유 기간 이름(시작 보유)", f"{api['holding'][0]} ~ {api['holding'][1]}", hold.get("보유 기간(데이터 첫날 전부터 ~ 기준일)"))
        eq("SPY 범례 숨김", "hidden" in ids["lg-bench"].attrs, True)
        d = api["daily"][-1]
        eq("누적 손익 읽기 줄(SPY 없음)", text("pl-read"), f"{d['date']}, 내 매매 {fkrw(d['pnl_krw'], True)}")
        eq("SPY 선 없음", len(ids["pl"].find(cls="l-bench")), 0)
    # 매매 줄: 증권사 칸, 지금 잔고 줄(날짜 칸 대신 앱 잔고 그대로, 평단 칸에 첫날 보유)
    rows = ids["rows"].find(cls="lrow")
    eq("매매 줄 수", len(rows), len(case["rows"]))
    steps = {x["trade_index"]: x for x in api["steps"]}
    for k, (row, tr) in enumerate(zip(rows, case["rows"])):
        picked = [o.attrs.get("value") for n in row.walk() if n.tag == "select" and n.attrs.get("data-k") == "broker"
                  for o in n.find("option") if "selected" in o.attrs]
        eq(f"{k + 1}번 줄 증권사", picked, [tr.get("broker", "")])
        avg = row.find(cls="avg")[0].text()
        st = steps.get(k)
        if tr["side"] == "HOLD":
            eq(f"{k + 1}번 줄 지금 잔고 표시", "hold" in (row.attrs.get("class") or "").split(), True)
            eq(f"{k + 1}번 줄 평단 칸(첫날 보유)", avg, f"첫날 {fnum(st['qty'])}주, {fprice(st['price'], cur)}" if st else "계산에서 빠짐")
        elif st and not st["excluded"]:
            eq(f"{k + 1}번 줄 평균 단가(그 증권사)", avg, fprice(st["avg_cost_after"], cur) if st["qty_after"] else "남은 수량 없음")
    lis = ids["hold-notes"].find("li")
    eq("계산 안내 수", len(lis), len(api["notes"]))
    for li, n in zip(lis, api["notes"]):
        if not li.text().endswith(n["message"] + n["code"]):
            bad.append(f"계산 안내 글: {li.text()!r}")
    # 가격 그림: 시작 보유는 매수 표시에서 뺌
    live = [x for x in api["steps"] if not x["excluded"] and not x.get("opening")]
    eq("가격 그림 매수 표시 수", len(ids["px"].find(cls="m-buy")), sum(x["side"] == "BUY" for x in live))
    return api, bad


def screen_trades(case, inputs):
    """화면에 넣을 매매(글자). check_api.api_trades와 같이 정답 파일의 inputs(체결가 규칙과 환율 배수를 푼 입력 값)를 씁니다."""
    out = []
    for k, t in enumerate(case["trades"]):
        u = inputs.get(str(k), t)
        row = {"date": u["d"], "side": u["side"], "qty": str(u["q"] if str(k) in inputs else t["q"])}
        if u.get("p") is not None:
            row["price"] = str(u["p"])
        if u.get("fx") is not None:
            row["fx_rate"] = str(u["fx"])
        out.append(row)
    return out


def write_saved(case, trades):
    TMP.write_text(json.dumps({"symbols": {case["symbol"]: {"asof": case["asof"], "trades": trades}},
                               "last": case["symbol"]}, ensure_ascii=False), encoding="utf-8")


def verdict(api):
    """결론 문장의 기대 글자와 숫자 색(화면 코드를 쓰지 않고 같은 규칙으로 따로 만듦). 요약이 없으면 (None, []).
    11/18 화면 개편: 원화 손익은 큰 숫자 칸(k-krw)이 맡고, 문장은 손익이 어디서 왔는지와 SPY 비교만(해요체, 금액은 fbig)."""
    s = api["summary"]
    if s is None:
        return None, []
    if not api["booked"]:
        return "장부에 들어간 매수가 없어 손익을 말할 수 없어요. 내 매매 탭의 경고를 봐 주세요.", []
    parts, tones = [], []

    def n(text, v):
        parts.append(text)
        tones.append(ftone(v))

    if api["currency"] == "KRW" and s["fx_effect_krw"] == 0 and s["cross_effect_krw"] == 0:
        parts.append("국내 종목이라 환율 효과 없이 모두 가격 효과예요.")
    else:
        for i, (name, key) in enumerate((("가격 효과", "price_effect_krw"), ("환율 효과", "fx_effect_krw"),
                                         ("교차 효과", "cross_effect_krw"))):
            parts.append(("" if i == 0 else ", ") + name + " ")
            n(fbig(s[key], True), s[key])
        gap = s["price_effect_krw"] + s["fx_effect_krw"] + s["cross_effect_krw"] - s["total_krw"]
        parts.append(f"으로 나뉘어요(원 단위 반올림으로 셋의 합과 {fkrw(abs(gap))} 차이)." if gap else "으로 나뉘어요.")
    if api["symbol"] != api["bench"]:
        d = s["total_minus_bench_krw"]
        parts.append(" 같은 날 같은 돈을 SPY에 넣었다면 ")
        n(fbig(s["bench_total_krw"], True), s["bench_total_krw"])
        if d == 0:
            parts.append("으로 이 매매와 같아요.")
        else:
            parts.append("이라, 이 매매가 ")
            n(fbig(abs(d)), d)
            parts.append(" 더 많아요." if d > 0 else " 더 적어요.")
    return "".join(parts).strip(), tones


def check_case(case, port, inputs, html=None):
    """화면 DOM과 API 값을 비교해 틀린 곳 목록을 돌려줍니다."""
    trades = screen_trades(case, inputs)
    api = post(port, {"symbol": case["symbol"], "asof": case["asof"], "trades": trades})
    if html is None:
        write_saved(case, trades)
        html = chrome_dom(f"http://127.0.0.1:{port}/")
    ids = dom(html).ids
    bad = []
    cur = api["currency"]
    usd = cur != "KRW"
    unit = "달러" if usd else "원"

    def text(i):
        return ids[i].text().strip() if i in ids else None

    def cls(node):
        return (node.attrs.get("class") or "").split()

    def eq(where, got, want):
        if got != want:
            bad.append(f"{where}: 화면 {got!r}, 기대 {want!r}")

    def toned(where, node, v):
        if ftone(v) not in cls(node):
            bad.append(f"{where} 색: {cls(node)}, 기대 {ftone(v)}")

    def card(where, i, want, v):
        eq(where, text(i), want)
        toned(where, ids[i], v)

    state = text("state") or ""
    boxes = [ids[i] for i in ("result", "result-more")]
    if (not re.match(r"계산 \d", state) or "err" in cls(ids["state"]) or any("hidden" in b.attrs for b in boxes)
            or any("stale" in cls(b) for b in boxes) or "통과" not in (text("pipe-pill") or "")):
        return api, [f"화면이 계산을 마치지 못함: {state!r}"]

    # 단위: 입력 칸 머리와 매도 표 머리
    eq("체결가 머리", text("h-price"), f"체결가({unit})")
    eq("평균 단가 머리", text("h-avg"), f"이 매매 뒤 평균 단가({unit})")
    eq("매도 가격 머리", text("h-sp"), f"가격({unit})")
    eq("매도 평균 단가 머리", text("h-sa"), f"팔기 전 평균 단가({unit})")

    # 2번 칸: 요약 카드와 보유 줄
    eq("흐림 안내 숨김", ["hidden" in ids[i].attrs for i in ("stale-note", "stale-note-2")], [True, True])
    s = api["summary"]
    want_v, want_tones = verdict(api)
    # 5번 칸 숨김: 요약이 없거나, 장부에 매수가 없거나, 국내 종목(환율 효과와 교차 효과가 0)이면 숨기고 결론 문장으로 대신함
    price_only = bool(s) and not usd and s["fx_effect_krw"] == 0 and s["cross_effect_krw"] == 0
    fx_off = s is None or not api["booked"] or price_only
    eq("나누기 칸 숨김", "hidden" in ids["fx-box"].attrs, fx_off)
    if want_v is None:
        eq("결론 문장(요약 없음)", "hidden" in ids["verdict"].attrs, True)
    else:
        eq("결론 문장 숨김", "hidden" in ids["verdict"].attrs, False)
        for kind, _ in violations(text("verdict") or "", quote_exempt=False):
            bad.append(f"결론 문장 금지 표현: {kind}")
        eq("결론 문장", text("verdict"), want_v)
        eq("결론 문장 색", [next((c for c in cls(b) if c in ("up", "down", "zero")), None)
                         for b in ids["verdict"].find("b")], want_tones)
    if s is None:
        for k in ("k-krw", "k-local", "k-bench", "k-mdd", "k-irr"):
            eq(f"{k}(요약 없음)", text(k), "–")
    else:
        card("원화 손익", "k-krw", fbig(s["total_krw"], True), s["total_krw"])
        head = " ".join(x for x in (fkrw(s["total_krw"], True) if shortened(s["total_krw"]) else "",
                                    "" if s["simple_return_pct"] is None else f"(단순 수익률 {fpct(s['simple_return_pct'])})") if x)
        eq("원화 손익 보조", text("k-krw-s"),
           (f"{head}. " if head else "") + f"실현 {fkrw(s['realized_krw'], True)}, 평가 {fkrw(s['unrealized_krw'], True)}")
        if usd:
            card("달러 손익", "k-local", flocal(s["total_local"], cur, True), s["total_local"])
            eq("달러 손익 보조", text("k-local-s"),
               f"실현 {flocal(s['realized_local'], cur, True)}, 평가 {flocal(s['unrealized_local'], cur, True)}")
        else:
            card("단순 수익률(카드)", "k-local", fpct(s["simple_return_pct"]), s["simple_return_pct"])
            eq("단순 수익률 보조", text("k-local-s"), f"원화 손익 / 넣은 돈 {fkrw(s['invested_krw'])}")
        d = s["total_minus_bench_krw"]
        card("SPY와 차이", "k-bench", fbig(d, True), d)
        eq("SPY에 넣었다면", text("k-bench-s"), (f"{fkrw(d, True)}, " if shortened(d) else "")
           + f"SPY에 넣었다면 {fkrw(s['bench_total_krw'], True)}" + ("(이 종목이 SPY)" if api["symbol"] == api["bench"] else ""))
        if s["max_drawdown_krw"] == 0:
            eq("최대 낙폭", text("k-mdd"), "없음")
        else:
            eq("최대 낙폭", text("k-mdd"), fbig(s["max_drawdown_krw"]))
            eq("최대 낙폭 보조", text("k-mdd-s"), (f"{fkrw(s['max_drawdown_krw'])}, " if shortened(s["max_drawdown_krw"]) else "")
               + "앞선 고점에서 가장 크게 내려간 폭")
            if "down" not in cls(ids["k-mdd"]):
                bad.append(f"최대 낙폭 색: {cls(ids['k-mdd'])}")
        if s["irr_pct"] is None:
            eq("연 환산", text("k-irr"), "계산 못 함")
            eq("연 환산이 없는 까닭(30일)", "30일" in (text("k-irr-s") or ""), api["irr_short"])
        else:
            card("연 환산", "k-irr", fpct(s["irr_pct"]), s["irr_pct"])
            eq("SPY 연 환산", text("k-irr-s"), f"SPY {fpct(s['bench_irr_pct'])}")
        hold = {}
        for sp in ids["holding-line"].find("span"):
            b = sp.find("b")
            if b:
                hold[sp.text()[: -len(b[0].text())].strip()] = b[0].text()
        want = {"남은 수량": f"{fnum(s['qty_left'])}주", "넣은 원화": fkrw(s["invested_krw"]),
                "단순 수익률": fpct(s["simple_return_pct"])}
        if s["qty_left"] > 0:
            want["평균 단가"] = flocal(s["avg_cost_local"], cur)
            if usd:
                want["평균 매수 환율"] = f"{fnum(s['avg_buy_fx'])}원"
        # 남은 주식 수익률(11/19, 증권사 앱의 「수익률」): API 값 그대로, 미국 종목은 원화 기준을 붙임
        if s.get("holding_return_pct") is not None:
            want["남은 주식 수익률(증권사 앱의 수익률)"] = fpct(s["holding_return_pct"]) + (
                f", 원화로 {fpct(s['holding_return_krw_pct'])}" if usd and s.get("holding_return_krw_pct") is not None else "")
        if api["holding"]:
            want["보유 기간(첫 매수일 ~ 기준일)"] = f"{api['holding'][0]} ~ {api['holding'][1]}"
        eq("보유 줄", hold, want)

        # 5번 칸: 원화 수익 나누기(숨김은 위에서 봄)
        vs = [] if fx_off else ids["fx"].find(cls="v")
        keys = ("price_effect_krw", "fx_effect_krw", "cross_effect_krw", "total_krw")
        eq("나누기 줄 수", len(vs), 0 if fx_off else 4)
        for n, key in zip(vs, keys):
            eq(f"나누기 {key}", n.text(), fkrw(s[key], True))
            toned(f"나누기 {key}", n, s[key])
        note = text("fx-note") or ""
        total3 = s["price_effect_krw"] + s["fx_effect_krw"] + s["cross_effect_krw"]
        if fx_off:
            pass
        elif total3 != s["total_krw"]:
            w = f"셋의 합({fkrw(total3, True)})과 원화 손익은 원 단위 반올림으로 {fkrw(abs(total3 - s['total_krw']))} 다릅니다."
            if w not in note:
                bad.append(f"나누기 합 문장 없음: {w!r}")
        elif "셋의 합" in note:
            bad.append("나누기 합이 같은데 다르다는 문장이 있음")
        if usd and s["naive_minus_total_krw"]:
            d = s["naive_minus_total_krw"]
            w = (f"달러 손익을 기준일 환율 하나로 바꾸면 {fkrw(s['naive_krw'], True)}으로, 실제 원화 손익보다 "
                 f"{fkrw(abs(d))} {'많게' if d > 0 else '적게'} 보입니다.")
            if w not in note:
                bad.append(f"기준일 환율 문장 없음: {w!r}")

    # 6번 칸: 매도별 손익. 매도가 없으면 표 대신 한 줄
    none = not api["sells"]
    eq("매도 표 숨김", ["hidden" in ids[i].attrs for i in ("sells-box", "sells-note", "sells-empty")], [none, none, not none])
    if none:
        live = {x["trade_index"] for x in api["steps"] if not x["excluded"]}
        dropped = any(t["side"] == "SELL" and k not in live for k, t in enumerate(trades))
        eq("매도 없음 문장", text("sells-empty"),
           "계산에 들어간 매도가 없어요. 빠진 매도 줄은 내 매매 탭의 경고를 봐 주세요." if dropped else "아직 판 적이 없어요.")
    trs = [tr for tr in ids["sells"].find("tr") if "empty" not in cls(tr)]
    eq("매도 줄 수", len(trs), len(api["sells"]))
    for tr, sl in zip(trs, api["sells"]):
        tds = tr.find("td")
        eq(f"매도 {sl['date']}", [td.text() for td in tds],
           [sl["date"], f"{fnum(sl['qty'])}주", fprice(sl["price"], cur), fprice(sl["avg_cost_before"], cur),
            fkrw(sl["realized_krw"], True)])
        toned(f"매도 {sl['date']} 원화 손익", tds[4], sl["realized_krw"])

    # 경고: 줄 번호, 날짜, 문구, 코드. 사례가 기대한 경고(cases_real.json)도 화면에 있는지
    lis = ids["warnings"].find("li")
    eq("경고 수", len(lis), len(api["warnings"]))
    for li, w in zip(lis, api["warnings"]):
        eq("경고 글", li.text(), f"{w['trade_index'] + 1}번 줄({w['trade_date']}): {w['message']}{w['code']}")
    if api["warnings"] and f"입력 경고 {len(api['warnings'])}건" not in state:
        bad.append(f"상태 줄(읽기 줄)에 경고 수가 없음: {state!r}")
    shown = {(li.find(cls="code")[0].text(), int(li.text().split("번 줄")[0]) - 1) for li in lis}
    for w in case.get("expect_warnings") or []:
        if (w["code"], w["trade"]) not in shown:
            bad.append(f"기대한 경고가 화면에 없음: {w['code']} {w['trade'] + 1}번 줄")

    # 1번 칸: 다시 열린 입력 값, 평균 단가, 비운 칸에 흐리게 보이는 값
    rows = ids["rows"].find(cls="lrow")
    eq("매매 줄 수", len(rows), len(trades))
    steps = {x["trade_index"]: x for x in api["steps"]}
    for k, (row, tr) in enumerate(zip(rows, trades)):
        inp = {n.attrs["data-k"]: n for n in row.walk() if n.tag == "input" and "data-k" in n.attrs}
        eq(f"{k + 1}번 줄 입력", {key: inp[key].attrs.get("value", "") for key in ("date", "qty", "price", "fx_rate")},
           {"date": tr["date"], "qty": tr["qty"], "price": tr.get("price", ""), "fx_rate": tr.get("fx_rate", "")})
        picked = lambda key: [o.attrs.get("value") for n in row.walk() if n.tag == "select" and n.attrs.get("data-k") == key
                              for o in n.find("option") if "selected" in o.attrs]
        eq(f"{k + 1}번 줄 구분", picked("side"), [tr["side"]])
        eq(f"{k + 1}번 줄 증권사", picked("broker"), [tr.get("broker", "")])
        avg = row.find(cls="avg")[0].text()
        st = steps.get(k)
        used = st is not None and not st["excluded"]
        if not used:
            eq(f"{k + 1}번 줄 평균 단가", avg, "계산에서 빠짐")
        elif st["qty_after"] == 0:
            eq(f"{k + 1}번 줄 평균 단가", avg, "남은 수량 없음")
        else:
            eq(f"{k + 1}번 줄 평균 단가", avg, fprice(st["avg_cost_after"], cur))
        if not tr.get("price"):
            eq(f"{k + 1}번 줄 비운 체결가", inp["price"].attrs.get("placeholder", ""),
               fprice(st["price"], cur) if used else "")
        if usd and not tr.get("fx_rate"):
            eq(f"{k + 1}번 줄 비운 환율", inp["fx_rate"].attrs.get("placeholder", ""),
               fnum(st["fx"]) if used and st["fx"] else "")

    # 3번 칸: 큰 움직임, 판정 전, 표시 수, 읽기 줄
    bm = api["big_moves"]
    got_dates = [li.text() for li in ids["bm-list"].find("li")]
    if bm and got_dates != bm["dates"]:
        bad.append(f"큰 움직임 날: 화면 {len(got_dates)}일, API {len(bm['dates'])}일")
    if bm:
        want = f"첫 매수일부터 기준일까지({api['holding'][0]} ~ {api['holding'][1]}) 큰 움직임 {len(bm['dates'])}일"
        if bm["not_judged_in_holding"]:
            want += f", {bm['judged_from'] or '데이터 끝'} 전은 판정 전"
        eq("큰 움직임 요약", text("bm-sum"), want)
    svg = ids["px"]
    eq("판정 전 띠", len(svg.find(cls="band")), 1 if bm and bm["not_judged_in_holding"] else 0)
    live = [x for x in api["steps"] if not x["excluded"]]
    counts = {"m-buy": sum(x["side"] == "BUY" for x in live), "m-sell": sum(x["side"] == "SELL" for x in live),
              "m-out": sum(x["excluded"] for x in api["steps"]), "m-big": len(bm["dates"]) if bm else 0}
    for c, want in counts.items():
        eq(f"가격 그림 {c} 수", len(svg.find(cls=c)), want)
    last_close = dict(api["prices"]).get(api["asof_price_date"])
    if f"종가 {fprice(last_close, cur)}" not in (text("px-read") or ""):
        bad.append(f"가격 읽기 줄: {text('px-read')!r}, 기대 종가 {fprice(last_close, cur)}")

    # 4번 칸: 누적 손익 읽기 줄과 최대 낙폭 띠
    if api["daily"]:
        d = api["daily"][-1]
        eq("누적 손익 읽기 줄", text("pl-read"),
           f"{d['date']}, 내 매매 {fkrw(d['pnl_krw'], True)}, SPY에 넣었다면 {fkrw(d['bench_krw'], True)}")
    pl = ids["pl"]
    dd = pl.find(cls="dd")
    if s is None or s["max_drawdown_krw"] == 0:
        eq("최대 낙폭 띠(낙폭 없음)", len(dd), 0)
    elif dd:
        lbl = [n.text() for n in pl.find(cls="lbl")]
        eq("최대 낙폭 띠 글", lbl, [f"최대 낙폭 {fkrw(s['max_drawdown_krw'])}"])
    return api, bad


# ---------- 대비 ----------

def tokens(css):
    """밝은 토큰과 어두운 토큰. 값이 var(--다른 토큰)이면 그 테마에서 풀어 씀(--up: var(--ok) 같은 것)."""
    pick = lambda t: dict(re.findall(r"--([\w-]+):\s*([^;]+);", t))
    light_raw = pick(re.search(r":root\s*\{(.*?)\}", css.split("@media")[0], re.S).group(1))
    dark_block = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\}', css, re.S).group(1)
    dark_raw = {**light_raw, **pick(dark_block)}

    def resolve(raw):
        out = {}
        for k in raw:
            v, seen = raw[k].strip(), set()
            while (m := re.fullmatch(r"var\(--([\w-]+)\)", v)) and m.group(1) not in seen:
                seen.add(m.group(1))
                v = raw[m.group(1)].strip()
            if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                out[k] = v
        return out
    return resolve(light_raw), resolve(dark_raw)


def dark_blocks_same(css):
    """시스템 설정(prefers-color-scheme)과 data-theme="dark"의 어두운 토큰이 같은지. 대비는 뒤쪽만 셉니다."""
    a = re.search(r':root:not\(\[data-theme="light"\]\)\s*\{(.*?)\}', css, re.S).group(1)
    b = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\}', css, re.S).group(1)
    pick = lambda t: dict(re.findall(r"--([\w-]+):\s*([^;]+);", t))
    return pick(a) == pick(b)


def lum(hexc):
    c = [int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# 화면에 실제로 놓이는 글자와 바탕의 짝(style.css에서 찾음)
PAIRS = [("ink", "bg"), ("ink", "surface"), ("ink-2", "bg"), ("ink-2", "surface"), ("muted", "bg"), ("muted", "surface"),
         ("muted", "surface-2"), ("accent", "surface"), ("up", "surface"), ("down", "surface"), ("up", "bg"),
         ("down", "bg"), ("ok", "ok-soft"), ("warn", "warn-soft"), ("info", "info-soft"), ("bad", "bad-soft"),
         ("ink", "warn-soft"), ("ink-2", "track"), ("ink", "surface-2"), ("muted", "warn-soft"), ("bad", "surface"),
         ("ink-2", "surface-2"),
         # 11/18 화면 개편 뒤 놓이는 짝: 흐린 글(ts-t3), 회색 타일(ts-fill), 큰 손익 숫자(up-big, down-big), 매수와 매도 범례(info, warn)
         ("ts-t3", "surface"), ("ts-t3", "bg"), ("ts-t3", "ts-fill"), ("ink-2", "ts-fill"), ("ink", "ts-fill"),
         ("up-big", "surface"), ("down-big", "surface"),
         ("up", "ts-fill"), ("down", "ts-fill"), ("info", "surface"), ("warn", "surface"), ("surface", "ink")]


def contrast():
    light, dark = tokens(CSS.read_text(encoding="utf-8"))
    rows = []
    for theme, tk in (("밝은 화면", light), ("어두운 화면", dark)):
        for fg, bg in PAIRS:
            r = ratio(tk[fg], tk[bg])
            rows.append({"theme": theme, "fg": fg, "bg": bg, "ratio": round(r, 2), "ok": r >= 4.5})
    return rows


# ---------- 고정 문구의 금지 표현(설계서 4절 「표현」) ----------

def phrases():
    """HTML 글과 스크립트 속 한국어 문구에 E04 채점기 규칙(따옴표 안 예외 켬)을 돌림. 화면 고정 문구에만 예외를 둠."""
    sys.path.insert(0, str(ROOT / "eval" / "e04_agent"))
    from score import violations

    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            self.skip += tag in ("script", "style")

        def handle_endtag(self, tag):
            self.skip -= tag in ("script", "style")

        def handle_data(self, data):
            if not self.skip and data.strip():
                self.out.append(data.strip())

    t = Text()
    t.feed((ROOT / "screen" / "index.html").read_text(encoding="utf-8"))
    texts = t.out
    for js in sorted((ROOT / "screen").glob("*.js")):
        for a, b in re.findall(r'"([^"]*[가-힣][^"]*)"|`([^`]*[가-힣][^`]*)`', js.read_text(encoding="utf-8")):
            texts.append(a or b)
    found = [{"text": x[:200], "kind": k} for x in texts for k, _ in violations(x, quote_exempt=True)]
    return {"phrases": len(texts), "violations": found}


# ---------- 실행 ----------

def _span(html, i, fn):
    """id가 i인 요소의 (여는 태그, 글자)를 fn으로 바꿈. 안에 다른 태그가 없는 요소만."""
    return re.sub(rf'(<[a-z]+ [^>]*\bid="{i}"[^>]*>)([^<]*)', lambda m: "".join(fn(m.group(1), m.group(2))), html,
                  count=1)


def _flip(tag):
    return tag.replace(" up", " @@").replace(" down", " up").replace(" @@", " down")


def mutations(api):
    """일부러 틀린 화면. 국내 사례와 미국 사례에 맞는 것만 돌려줌."""
    s, usd = api["summary"], api["currency"] != "KRW"
    m = {
        "원화 손익 1원 바꿈": lambda h: _span(h, "k-krw", lambda t, x: (t, fkrw(s["total_krw"] + 1, True))),
        "원화 손익 색 뒤집기": lambda h: _span(h, "k-krw", lambda t, x: (_flip(t), x)),
        "원화 손익 단위를 $로": lambda h: _span(h, "k-krw", lambda t, x: (t, x.replace("원", "").replace("+", "+$"))),
        "원화 손익 부호 지우기": lambda h: _span(h, "k-krw", lambda t, x: (t, x.lstrip("+" + MINUS))),
        "SPY 차이 단위를 %로": lambda h: _span(h, "k-bench", lambda t, x: (t, x.replace("원", "%"))),
        "연 환산 % 지우기": lambda h: _span(h, "k-irr", lambda t, x: (t, x.replace("%", ""))),
        "매도 손익 줄 지움": lambda h: re.sub(r'(<tbody id="sells">.*?)<tr>.*?</tr>', r"\1", h, count=1, flags=re.S),
        "매도 수량 단위 주를 원으로": lambda h: re.sub(r'(<tbody id="sells">.*?<td[^>]*>[^<]*</td><td[^>]*>[^<]*)주',
                                             r"\1원", h, count=1, flags=re.S),
        "큰 움직임 하루 지움": lambda h: re.sub(r'(<ul id="bm-list"[^>]*>)<li>[^<]*</li>', r"\1", h, count=1),
        "판정 전 띠 지움": lambda h: re.sub(r'<rect class="band"[^>]*>(</rect>)?', "", h, count=1),
        "평균 단가 칸 바꿈": lambda h: re.sub(r'(<span class="avg"[^>]*>[^<]*?)(\d)(</span>)',
                                       lambda x: x.group(1) + str((int(x.group(2)) + 1) % 10) + x.group(3), h, count=1),
        "다시 열린 수량 바꿈": lambda h: re.sub(r'(data-k="qty"[^>]*value=")(\d+)', lambda x: x.group(1) + x.group(2) + "0",
                                         h, count=1),
        "체결가 머리 단위 바꿈": lambda h: _span(h, "h-price", lambda t, x: (t, "체결가")),
        "결론 문장 첫 숫자 바꿈": lambda h: re.sub(r'(<p id="verdict"[^>]*>[^<]*<b[^>]*>)([^<]*)',
                                           lambda x: x.group(1) + x.group(2).replace("원", "0원", 1), h, count=1),
        "원화 손익 보조 1원 바꿈": lambda h: _span(h, "k-krw-s", lambda t, x: (t, x.replace(
            fkrw(s["realized_krw"], True), fkrw(s["realized_krw"] + 1, True), 1))),
        "결론 문장 색 뒤집기": lambda h: re.sub(r'(<p id="verdict"[^>]*>[^<]*)<b class="n (up|down)"',
                                         lambda x: x.group(1) + '<b class="n ' + ("down" if x.group(2) == "up" else "up") + '"',
                                         h, count=1),
    }
    if api["symbol"] != api["bench"] and s["total_minus_bench_krw"] != 0:  # SPY 자체는 비교 문장이 없음
        m["결론 문장 많고 적음 뒤집기"] = lambda h: re.sub(
            r'(<p id="verdict".*?) 더 (많|적)아요\.', lambda x: x.group(1) + (" 더 적" if x.group(2) == "많" else " 더 많") + "아요.",
            h, count=1, flags=re.S)
    if api["sells"]:
        m["매도 표 숨김"] = lambda h: h.replace('<div id="sells-box" class="tablebox">', '<div id="sells-box" class="tablebox" hidden="">', 1)
    if usd:
        m["달러 손익 단위를 원으로"] = lambda h: _span(h, "k-local", lambda t, x: (t, x.replace("$", "") + "원"))
        m["나누기 칸 환율 효과 1원 바꿈"] = lambda h: re.sub(
            r'(<div id="fx"[^>]*>(?:.*?<span class="v[^"]*">){1}.*?<span class="v[^"]*">)[^<]*',
            lambda x: x.group(1) + fkrw(s["fx_effect_krw"] + 1, True), h, count=1, flags=re.S)
    else:
        m["단순 수익률 % 지우기"] = lambda h: _span(h, "k-local", lambda t, x: (t, x.replace("%", "")))
        m["국내 종목에 나누기 칸 보임"] = lambda h: h.replace('<section id="fx-box" class="x-card fx-box" aria-labelledby="fx-h" hidden="">',
                                                      '<section id="fx-box" class="x-card fx-box" aria-labelledby="fx-h">', 1)
    return m


def selftest(port):
    """대조기가 화면의 틀린 값을 잡는지: 국내 사례(R02)와 미국 사례(R03)의 DOM을 일부러 바꿔 넣음."""
    cases = {c["id"]: c for c in json.loads(CASES.read_text(encoding="utf-8"))}
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    out = {}
    for cid in ("R02", "R03"):
        case, inputs = cases[cid], expected[cid]["inputs"]
        write_saved(case, screen_trades(case, inputs))
        html = chrome_dom(f"http://127.0.0.1:{port}/")
        api, bad = check_case(case, port, inputs, html)
        assert not bad, (cid, bad)
        for name, fn in mutations(api).items():
            h = fn(html)
            if h == html:
                out[f"{cid} {name}"] = None  # 시험을 만들지 못함(그 사례에 그 칸이 없음)
                continue
            _, b = check_case(case, port, inputs, h)
            out[f"{cid} {name}"] = bool(b)
    acases = {c["id"]: c for c in ACCOUNT_CASES}
    for cid, muts in ACCOUNT_MUT.items():
        case = acases[cid]
        write_saved(case, case["rows"])
        html = chrome_dom(f"http://127.0.0.1:{port}/")
        _, bad = check_account_case(case, port, html)
        assert not bad, (cid, bad)
        for name, fn in muts.items():
            h = fn(html)
            if h == html:
                out[f"{cid} {name}"] = None
                continue
            _, b = check_account_case(case, port, h)
            out[f"{cid} {name}"] = bool(b)
    extra = {c["id"]: c for c in EXTRA}
    for cid, muts in EXTRA_MUT.items():
        case = extra[cid]
        write_saved(case, screen_trades(case, {}))
        html = chrome_dom(f"http://127.0.0.1:{port}/")
        api, bad = check_case(case, port, {}, html)
        assert not bad, (cid, bad)
        for name, fn in muts.items():
            h = fn(html)
            if h == html:
                out[f"{cid} {name}"] = None
                continue
            _, b = check_case(case, port, {}, h)
            out[f"{cid} {name}"] = bool(b)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if not Path(CHROME).exists():
        print("Chrome이 없어 화면 대조를 하지 못했습니다(실패로 셈)")
        return 1
    TMP.parent.mkdir(parents=True, exist_ok=True)
    httpd, _ = serve(screen_data.OUT, 0, TMP)
    port = httpd.server_address[1]
    try:
        if a.selftest:
            res = selftest(port)
            for k, v in res.items():
                print(f"  {'잡음' if v else '만들지 못함' if v is None else '못 잡음'}  {k}")
            ok = all(v is True for v in res.values())
            print(f"자체 시험 {sum(v is True for v in res.values())}/{len(res)} 잡음 → " + ("통과" if ok else "실패"))
            return 0 if ok else 1
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        if a.only:
            cases = [c for c in cases if c["id"] in a.only.split(",")]
        results, t0 = [], time.time()
        for c in cases:
            _, bad = check_case(c, port, expected[c["id"]]["inputs"])
            results.append({"id": c["id"], "ok": not bad, "bad": bad})
            print(f"  {'같음' if not bad else '다름'}  {c['id']} {c['title']}" + ("".join(f"\n      {b}" for b in bad)))
        extra = []
        if not a.only:
            for c in EXTRA:
                _, bad = check_case(c, port, {})
                extra.append({"id": c["id"], "ok": not bad, "bad": bad})
                print(f"  {'같음' if not bad else '다름'}  {c['id']} {c['title']}(개발 사례)" + ("".join(f"\n      {b}" for b in bad)))
            for c in ACCOUNT_CASES:
                _, bad = check_account_case(c, port)
                extra.append({"id": c["id"], "ok": not bad, "bad": bad})
                print(f"  {'같음' if not bad else '다름'}  {c['id']} {c['title']}(개발 사례)" + ("".join(f"\n      {b}" for b in bad)))
        con = contrast()
        worst = {th: min((r for r in con if r["theme"] == th), key=lambda r: r["ratio"]) for th in ("밝은 화면", "어두운 화면")}
        for th, r in worst.items():
            print(f"  대비 {th}: 가장 낮은 짝 {r['fg']}/{r['bg']} {r['ratio']}:1")
        for r in con:
            if not r["ok"]:
                print(f"  대비 모자람: {r['theme']} {r['fg']}/{r['bg']} {r['ratio']}:1")
        same_dark = dark_blocks_same(CSS.read_text(encoding="utf-8"))
        print(f"  어두운 화면 두 블록 {'같음' if same_dark else '다름'}")
        ph = phrases()
        for v in ph["violations"]:
            print(f"  금지 표현({v['kind']}): {v['text']}")
        ok_cases = sum(r["ok"] for r in results)
        ok_extra = sum(r["ok"] for r in extra)
        ok = (ok_cases == len(results) and ok_extra == len(extra) and all(r["ok"] for r in con) and same_dark
              and not ph["violations"])
        OUT.write_text(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases": results, "extra": extra, "contrast": con,
                                   "dark_blocks_same": same_dark, "phrases": ph, "seconds": round(time.time() - t0, 1)},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"화면 값 대조 {ok_cases}/{len(results)}" + (f", 개발 사례 {ok_extra}/{len(extra)}" if extra else "") + f", 대비 {sum(r['ok'] for r in con)}/{len(con)}, "
              f"고정 문구 금지 표현 {len(ph['violations'])}건({ph['phrases']}개 문구) → {'통과' if ok else '실패'}")
        return 0 if ok else 1
    finally:
        stop(httpd)
        TMP.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
