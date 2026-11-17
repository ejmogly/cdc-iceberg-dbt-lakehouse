"""E02 「그때 무슨 일이」 답 쓰기 판 1(docs/ai/experiments/E02-rag.md 3절, 4절).

질문은 한 가지: 「(종목)이 (날짜)에 크게 움직인 날 무슨 일이 있었나」. 답은 두 부분입니다.

1. 움직임 문장(코드): 날짜, 하루 등락률, 최근 60거래일 표준편차의 몇 배인지, 큰 움직임 날인지. 숫자는 코드가 셉니다
   (3절 「숫자 일치」). 큰 움직임의 식은 3절과 같습니다(수정한 가격의 단순 수익률, 그날을 뺀 앞 60거래일 표본 표준편차의 2배 초과).
2. 기사 요약(모델): 검색 판(ai/news_search.py)이 고른 상위 5건 가운데 그 종목이나 시장의 일을 전한 기사만 1 ~ 4문장으로,
   문장마다 기사 번호를 붙여서. 모델이 쓴 글은 코드가 검사합니다: 인용 번호(1 ~ 5 안), 인과 표현(채점기 규칙),
   숫자(인용한 기사 글에 있는 숫자만). 걸리면 짚어서 한 번 다시 쓰고, 그래도 걸리면 코드 문구로 끝냅니다.

근거가 없으면(창에 기사가 없거나 평가에서 근거를 뺀 날) 모델을 부르지 않고 코드 문구 「찾지 못했습니다」로 답합니다
(3절 「미리 예상하는 실패」: 근거 없음 분기를 코드로). 일정은 이 판의 근거에 넣지 않습니다(H3에서 더함). 화면 7번 칸은
일정을 코드가 따로 보입니다.
"""
import json
import re
import statistics
import sys
import time
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ai import llm, news_search

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "e04_agent"))
from score import text_numbers, violations  # noqa: E402  채점기와 같은 규칙

RULES = (Path(__file__).parent / "prompts" / "e02_write_v2.txt").read_text(encoding="utf-8")  # v1은 「360,000」을 「36만」으로 옮김
LOOKBACK, FIRST = 60, 61
NAMES = {"SPY": ("SPDR S&P 500 ETF", "미국 증시 전체"), "QQQ": ("Invesco QQQ", "나스닥과 미국 기술주"),
         "IWM": ("iShares 러셀 2000 ETF", "미국 소형주"), "SHY": ("iShares 1 ~ 3년 미국 국채 ETF", "미국 국채 금리와 연준"),
         "TLT": ("iShares 20년 이상 미국 국채 ETF", "미국 국채 금리와 연준"),
         "HYG": ("iShares 하이일드 회사채 ETF", "하이일드 회사채"), "GLD": ("SPDR 금 ETF", "금 시세"),
         "USO": ("미국 원유 펀드", "원유 시세"), "UUP": ("Invesco 미국 달러 강세 ETF", "달러 가치"),
         "EFA": ("iShares MSCI EAFE ETF", "유럽, 일본 같은 선진국 증시"), "EEM": ("iShares MSCI 신흥국 ETF", "신흥국 증시"),
         "IBIT": ("iShares 비트코인 ETF", "비트코인 시세"), "NVDA": ("NVIDIA", None), "AAPL": ("Apple", None),
         "TSLA": ("Tesla", None), "GOOGL": ("Alphabet", None), "TSM": ("TSMC", None)}
SCHEMA = {"type": "object", "required": ["found", "text", "citations"], "properties": {
    "found": {"type": "boolean"}, "text": {"type": "string"}, "citations": {"type": "array", "items": {"type": "integer"}}}}
TEXT = {"not_found": "그날의 검색 창에서 이 종목이나 시장의 일을 전한 기사를 찾지 못했습니다.",
        "not_verified": "기사 내용을 규칙에 맞게 정리하지 못해 보이지 않습니다.",
        "llm_failed": "기사 내용을 정리하지 못했습니다(모델 호출 실패).",
        "first_days": "데이터 첫 61거래일이라 큰 움직임인지 판정하지 않습니다."}
CITE = re.compile(r"\[(\d+)\]")
STRICT = re.compile(r"따라서|그러므로|때문|탓|덕분|원인|여파|계기로|영향")


def _half_up(x, places):
    return Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _signed(v):
    s = f"{abs(v):.2f}"
    return ("+" if v > 0 else "−" if v < 0 else "") + s


def day_move(rows, d):
    """rows: {날짜: {"adj_close" 또는 "close"}}. {"pct", "ratio", "big", "judged"}. pct는 % 소수 둘째, ratio는 소수 첫째 자리
    (0.5는 0에서 먼 쪽). 앞 거래일이 61개 안 되면 judged=False(3절, 화면 3번 칸과 같음)."""
    days = sorted(rows)
    if d not in rows:
        raise ValueError(f"{d}는 거래일이 아닙니다")
    i = days.index(d)
    px = [float(rows[x]["adj_close"] if rows[x].get("adj_close") is not None else rows[x]["close"]) for x in days]
    r = px[i] / px[i - 1] - 1 if i else None
    if i < FIRST:
        return {"pct": None if r is None else float(_half_up(r * 100, 2)), "ratio": None, "big": None, "judged": False}
    rets = [px[k] / px[k - 1] - 1 for k in range(i - LOOKBACK, i)]
    sd = statistics.stdev(rets)
    return {"pct": float(_half_up(r * 100, 2)), "ratio": float(_half_up(abs(r) / sd, 1)), "big": abs(r) > 2 * sd,
            "judged": True}


def move_line(sym, d, mv):
    name = NAMES[sym][0]
    if not mv["judged"]:
        return f"{d} {name}({sym})의 하루 등락률은 {_signed(mv['pct'])}%입니다. {TEXT['first_days']}"
    tail = "큰 움직임 날입니다" if mv["big"] else "크게 움직인 날은 아닙니다"
    return (f"{d} {name}({sym})의 하루 등락률은 {_signed(mv['pct'])}%로, 그날 앞 60거래일 등락률 표준편차의 "
            f"{mv['ratio']:.1f}배입니다. {tail}(2배 초과가 큰 움직임).")


def check_text(text, cited_text):
    """(금지 표현 문장, 근거 없는 숫자). 숫자는 인용한 기사 제목과 요약에 있어야 합니다(v2 규칙의 글 속 숫자)."""
    t = re.sub(r"([.!?])(?=[^\s\d])", r"\1 ", text or "")
    bad = [k for k, _ in violations(CITE.sub("", t))] + (["strict"] if STRICT.search(CITE.sub("", t)) else [])
    pool = [v for v, _ in text_numbers(cited_text, "v2")]
    # 분기와 반기 표기(Q1, first quarter, H2)는 종목 코드처럼 지워지므로 그 숫자를 근거에 더함(「1분기」로 옮겨도 됨)
    pool += [float(m) for m in re.findall(r"\b[QH]([1-4])\b", cited_text)]
    pool += [float(i) for i, w in enumerate(("first", "second", "third", "fourth"), 1)
             if re.search(rf"\b{w}[- ](?:quarter|half)\b", cited_text, re.I)]
    nums = text_numbers(CITE.sub(" ", text or ""), "v2")  # 숫자는 나누기 전 글에서(소수점을 문장 끝으로 보지 않게)
    untraced = [v for v, p in nums if not any(abs(abs(v) - abs(y)) <= 0.5 * 10 ** -p + 1e-9 for y in pool)]
    return bad, untraced


def _cites_ok(out, k):
    if not isinstance(out, dict):
        return False
    nums = [int(n) for n in CITE.findall(out.get("text") or "")]
    cites = [int(n) for n in out.get("citations") or []]
    if out.get("found"):  # 글의 인용 번호와 적은 인용 번호가 모두 1 ~ k 안이고, 글에 번호가 하나라도 있음
        return bool(nums) and all(1 <= n <= k for n in nums + cites)
    return not nums


def _prompt(sym, d, arts):
    name, market = NAMES[sym]
    lines = [f"Trading day: {d}. Symbol: {sym} ({name}). " + (f"It tracks: {market}." if market else "Individual company."),
             "", "Articles:"]
    for n, a in enumerate(arts, 1):
        lines.append(f"[{n}] {a['time']} | {a['publisher']}\n    title: {a['title']}\n    summary: {a['summary']}")
    return "\n".join(lines)


def answer(con, days, rows, sym, d, version="h1", drop_evidence=False, tag=""):
    """(답, 기록). 답: {"status", "text", "move", "citations": [기사], "found"}. rows는 그 종목의 가격 행(수정한 종가)."""
    t0, calls = time.perf_counter(), []
    mv = day_move(rows, d)
    head = move_line(sym, d, mv)
    arts, n_window = [], 0
    if not drop_evidence:
        arts, n_window, _ = news_search.search(con, days, date.fromisoformat(d), sym, version)
    trace = {"version": version, "window_articles": n_window, "retrieved": [a["id"] for a in arts], "llm": calls,
             "decided_by": None, "rewrite": None}

    def done(status, body, cites=(), found=False):
        trace["seconds"] = time.perf_counter() - t0
        return {"status": status, "text": f"{head} {body}".strip(), "move": mv, "found": found,
                "citations": [{"n": n, "id": arts[n - 1]["id"], "title": arts[n - 1]["title"],
                               "publisher": arts[n - 1]["publisher"], "time": arts[n - 1]["time"]} for n in cites]}, trace

    if not arts:
        trace["decided_by"] = "code:no_evidence"
        return done("unknown", TEXT["not_found"])

    def ask(prompt, t):
        r = llm.call(prompt, RULES, schema=SCHEMA, tag=t, kind="real")
        calls.append({k: r[k] for k in ("cost_usd", "seconds", "usage", "error")})
        return r["output"]

    prompt = _prompt(sym, d, arts)
    out = ask(prompt, f"e02-write:{tag}")
    if out is None:
        trace["decided_by"] = "code:llm_failed"
        return done("unknown", TEXT["llm_failed"])

    def verdict(o):
        if not _cites_ok(o, len(arts)):
            return ["citation"], []
        if not o.get("found"):
            return [], []
        cited = " ".join(f"{arts[n - 1]['title']} {arts[n - 1]['summary']}" for n in sorted(set(map(int, CITE.findall(o["text"])))))
        return check_text(o["text"], cited)

    bad, untraced = verdict(out)
    if bad or untraced:
        trace["rewrite"] = {"before": out, "violations": bad, "untraced": untraced}
        fix = (prompt + "\n\nYour previous answer (JSON):\n" + json.dumps(out, ensure_ascii=False) +
               f"\n\nIt breaks these rules: {bad or ''} {('numbers not in the cited articles: ' + str(untraced)) if untraced else ''}."
               "\nRewrite. Cite only article numbers 1 to " + str(len(arts)) + ", use no cause words, and copy numbers only from the cited article.")
        out2 = ask(fix, f"e02-rewrite:{tag}")
        bad2, untraced2 = verdict(out2) if out2 is not None else (["llm_failed"], [])
        trace["rewrite"].update(after_violations=bad2, after_untraced=untraced2)
        if out2 is None or bad2 or untraced2:
            trace["decided_by"] = "code:not_verified"
            return done("unknown", TEXT["not_verified"])
        out = out2
    if not out.get("found"):
        trace["decided_by"] = "model:not_found"
        return done("unknown", TEXT["not_found"])
    cites = sorted(set(map(int, CITE.findall(out["text"]))))
    return done("answered", out["text"].strip(), cites, found=True)
