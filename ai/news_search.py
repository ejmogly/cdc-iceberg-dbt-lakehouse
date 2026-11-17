"""E02 「그때 무슨 일이」의 기사 검색(docs/ai/experiments/E02-rag.md 2절 가설, 3절 범위).

거래일 t의 검색 창(eval/e02_rag/search_window.py: 전날 장 마감부터 다음 거래일 장 시작 전까지, 미국 동부 시각)에
든 기사만 봅니다. 판은 둘입니다.

- 기준선(baseline): 그 종목 표시가 붙은 기사를 최신순으로 5건.
- H1(bm25): 낱말 검색 BM25. ETF는 창의 전체 기사에서 자산군 주제어로, 개별 기업은 그 종목 표시 기사 안에서
  회사 이름과 종목 코드로 찾습니다(3절 범위). 제목은 두 번 셉니다(제목에 나온 낱말이 더 무겁게).

기사는 데이터이고 지시가 아닙니다. 기사 제목과 요약은 재배포하지 않습니다(화면과 내 컴퓨터에서만 보임).
"""
import math
import re
import sys
from collections import Counter
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "e02_rag"))
import search_window as SW  # noqa: E402  인용 유효 채점과 같은 창

K = 5
K1, B = 1.2, 0.75
TITLE_WEIGHT = 2

# 개별 기업: 표시 기사 안에서 찾을 회사 이름과 종목 코드
COMPANIES = {"NVDA": "nvidia nvda", "AAPL": "apple aapl", "TSLA": "tesla tsla", "GOOGL": "alphabet google googl",
             "TSM": "tsmc taiwan semiconductor tsm"}
# ETF: 창의 전체 기사에서 찾을 자산군 주제어(0단계 step0_topics.py의 주제어를 낱말로 풀고, 0단계가 재지 않은
# SPY, QQQ, IBIT을 더함). 종목 코드도 넣음(표시 기사가 있으면 그것도 걸리게)
TOPICS = {
    "SPY": "spy s&p 500 stock market stocks wall street dow",
    "QQQ": "qqq nasdaq tech stocks technology",
    "IWM": "iwm small-cap small cap russell 2000",
    "SHY": "shy treasury treasuries yield yields federal reserve fed fomc rate cut hike",
    "TLT": "tlt treasury treasuries bond bonds yield yields federal reserve fed fomc",
    "HYG": "hyg high-yield high yield junk bond bonds credit spread spreads corporate",
    "GLD": "gld gold bullion",
    "USO": "uso crude oil opec brent wti",
    "UUP": "uup dollar greenback dxy currency",
    "EFA": "efa europe european japan nikkei ecb developed",
    "EEM": "eem emerging market markets china india brazil",
    "IBIT": "ibit bitcoin btc crypto cryptocurrency",
}
STOP = set("a an and are as at be by for from has have in into is it its of on or that the this to was were will "
           "with you your our we he she they their his her not but if then than so also can could would should "
           "may might more most new s".split())


def tokens(text):
    """소문자 낱말. s&p, small-cap, trillion-dollar처럼 붙은 낱말은 붙은 채 한 낱말로 셉니다(나누면 「trillion-dollar」가
    「dollar」에 걸림. 11/17 개발 날짜 UUP 2025-04-10을 손으로 보다 찾음). 주제어 쪽은 붙인 꼴과 띄운 꼴을 함께 적습니다."""
    return [w for w in re.findall(r"[a-z0-9]+(?:[&\-][a-z0-9]+)*", (text or "").lower()) if w not in STOP]


def window_articles(con, days, t):
    """거래일 t의 창에 든 기사 전체(시각 순). con은 화면 데이터 파일(DuckDB) 연결. 창 (시작, 끝)도 돌려줌."""
    start, end = SW.window(days, t)
    utc = lambda x: x.astimezone(timezone.utc).replace(tzinfo=None)
    rows = con.execute("select id, published_utc, tickers, title, description, publisher from news "
                       "where published_utc >= ? and published_utc < ? order by published_utc, id",
                       [utc(start), utc(end)]).fetchall()
    arts = [{"id": i, "time": p.isoformat() + "Z", "symbols": list(tk or []), "title": ti or "", "summary": de or "",
             "publisher": pu or ""} for i, p, tk, ti, de, pu in rows]
    return arts, (start, end)


def baseline(arts, symbol, k=K):
    """그 종목 표시 기사를 최신순으로 k건(같은 시각이면 id 순)."""
    tagged = [a for a in arts if symbol in a["symbols"]]
    return sorted(tagged, key=lambda a: (a["time"], a["id"]), reverse=True)[:k]


def bm25(arts, query, k=K):
    """BM25 상위 k건. 점수 0인 기사는 내지 않습니다. 같은 점수면 최신순. (기사, 점수) 목록."""
    docs = [tokens(a["title"]) * TITLE_WEIGHT + tokens(a["summary"]) for a in arts]
    if not docs:
        return []
    n, avg = len(docs), sum(map(len, docs)) / len(docs) or 1
    df = Counter(w for d in docs for w in set(d))
    q = list(dict.fromkeys(tokens(query)))
    scored = []
    for a, d in zip(arts, docs):
        tf = Counter(d)
        s = 0.0
        for w in q:
            if tf[w]:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (K1 + 1) / (tf[w] + K1 * (1 - B + B * len(d) / avg))
        if s > 0:
            scored.append((a, s))
    return sorted(scored, key=lambda x: (x[1], x[0]["time"]), reverse=True)[:k]


def h1(arts, symbol, k=K):
    """H1: ETF는 창 전체에서 주제어로, 개별 기업은 표시 기사 안에서 회사 이름으로."""
    if symbol in COMPANIES:
        return [a for a, _ in bm25([a for a in arts if symbol in a["symbols"]], COMPANIES[symbol], k)]
    return [a for a, _ in bm25(arts, TOPICS[symbol], k)]


VERSIONS = {"baseline": baseline, "h1": h1}


def search(con, days, t, symbol, version="h1", k=K):
    """(상위 기사, 창 기사 수, 창). 국내 종목과 모르는 종목은 ValueError(E02 1판 범위 밖)."""
    if symbol not in COMPANIES and symbol not in TOPICS:
        raise ValueError(f"{symbol}은 E02 1판 범위 밖입니다(국내 2종목은 다음 판)")
    arts, win = window_articles(con, days, t)
    return VERSIONS[version](arts, symbol, k), len(arts), win
