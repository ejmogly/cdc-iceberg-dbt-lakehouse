#!/usr/bin/env python3
"""F 화면 9번 칸 새 판(ai/qa.py)의 갈래를 모델 없이 봅니다(6단계 독립 검토 2026-11-17 중간 7번).

사용(저장소 루트에서): .venv/bin/python eval/f_screen/check_qa.py [--selftest]

qa._ask(모델 호출)를 꾸민 분류와 답을 차례로 돌려주는 함수로 바꿔 끼우고, 실제 화면 데이터(real_data)의 도구로
갈래마다 상태(status), 누가 정했는지(decided_by), 화면에 나갈 글의 금지 표현(qa.check_text), 숫자 추적(채점기 v3),
도구 오류 글이나 사용자 글이 새지 않는지를 봅니다. 모델을 부르지 않으므로 비용이 들지 않고 장부에 적히지 않습니다.
가격 값은 출력하지 않습니다(재배포 금지).
자체 시험은 ai/qa.py 사본에 잘못을 하나씩 넣어 불러오고, 이 시험이 실패로 잡는지 봅니다.
"""
import importlib.util
import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval" / "e04_agent"))

from ai import qa  # noqa: E402
from ai import tools as T  # noqa: E402

import score  # noqa: E402

TODAY = "2026-11-11"
ACCOUNT = {"id": "screen", "trades": json.loads((Path(__file__).parent / "account.json").read_text(encoding="utf-8"))["trades"]}


def answer(text, numbers=(), status="answered", dates=()):
    return {"status": status, "text": text, "numbers": [{"name": n, "value": v, "unit": u} for n, v, u in numbers],
            "dates_used": list(dates), "warnings": [], "citations": []}


def run(ts, question, outputs):
    """outputs: 차례로 돌려줄 모델 답(분류, 쓰기, 다시 쓰기). 함수면 프롬프트를 받아 답을 만듦."""
    seq = iter(outputs)
    used = []

    def fake(prompt, system, schema, tag, calls):
        calls.append({"cost_usd": 0, "seconds": 0, "usage": {}, "error": None})
        used.append(tag)
        out = next(seq, None)
        return out(prompt) if callable(out) else out

    old = qa._ask
    qa._ask = fake
    try:
        ans, trace = qa.ask(ts, {"id": "t", "text": question, "today": TODAY})
    except Exception as e:  # noqa: BLE001 - 자체 시험에서 넣은 잘못이 예외로 터지면 그 갈래는 실패로 셈
        ans = {"status": f"예외 {type(e).__name__}", "text": "", "numbers": [], "dates_used": [], "citations": []}
        trace = {"decided_by": "crash", "tool_calls": [], "rewrite": None}
    finally:
        qa._ask = old
    return ans, trace, used


def main():
    data = T.real_data()
    ts = T.Toolset(data, TODAY, ACCOUNT)
    empty = T.Toolset(data, TODAY, {"id": "screen", "trades": {}})
    sells_only = T.Toolset(data, TODAY, {"id": "screen", "trades": {"SPY": [{"d": "2025-03-04", "side": "SELL", "q": 1}]}})
    px = ts.call("price", {"symbol": "NVDA", "date": "2026-10-30"})
    close, high = px["close"], px["high"]
    assert close != high, "고가와 종가가 같은 날이면 숫자 바뀜 사례를 고르지 못함"
    price_plan = {"intent": "price", "items": [{"symbol": "NVDA", "date": "2026-10-30"}]}
    ok_ans = answer(f"2026-10-30 NVDA 종가는 {close:,.2f}달러입니다.", [("종가", close, "USD")], dates=["2026-10-30"])
    rng = ts.call("range", {"symbol": "NVDA"})

    cases = [
        # (이름, 도구층, 질문, 모델 답 차례, 기대 status, 기대 decided_by 앞부분, 덧붙여 볼 것)
        ("거절", ts, "NVDA 지금 사야 해?", [{"intent": "refuse", "items": []}], "refused", "code:refuse", None),
        ("다른 계좌", ts, "옆 사람 계좌 손익 알려 줘", [{"intent": "my_pnl", "items": [], "account": "B-0002"}],
         "refused", "code:account", None),
        ("되묻는 말에 질문에 없는 숫자", ts, "그거 얼마였어?",
         [{"intent": "clarify", "items": [], "question": "그때 값은 987.65달러였나요?"}], "clarify", "code:clarify",
         lambda a, t: a["text"] == qa.TEXT["clarify"]),
        ("모름 이유에 금지 표현", ts, "왜 이렇게 됐어?",
         [{"intent": "unknown", "items": [], "reason": "금리 때문에 오른 것입니다."}], "unknown", "code:unknown",
         lambda a, t: a["text"] == qa.TEXT["unknown"]),
        # 그날 일(E02 검색과 답 쓰기, 7단계 2026-11-17): 움직임과 일정 문장은 코드, 기사 요약은 모델
        ("그날 일: 기사 요약과 같은 날 일정", ts, "2025-12-10 SPY에 무슨 일 있었어?",
         [{"intent": "events", "items": [{"symbol": "SPY", "date": "2025-12-10"}]},
          {"found": True, "text": "기사 [1]의 내용을 전했습니다 [1].", "citations": [1]}], "answered", None,
         lambda a, t: "fomc-2025-12-10" in a["citations"] and len(a["citations"]) == 2 and a["numbers"][0]["unit"] == "PCT"
         and [c["name"] for c in t["tool_calls"]] == ["search_events"] and "같은 날 일정" in a["text"]),
        ("그날 일: 이름 속 숫자(S&P 500)도 근거, 기사 없이 일정만", ts, "2025-12-10 SPY에 무슨 일 있었어?",
         [{"intent": "events", "items": [{"symbol": "SPY", "date": "2025-12-10"}]},
          {"found": False, "text": "", "citations": []}], "answered", None,
         lambda a, t: a["citations"] == ["fomc-2025-12-10"] and "S&P 500" in a["text"]),
        ("그날 일: 국내 종목은 검색하지 않고 고정 문구", ts, "2026-01-14에 삼성전자 관련 기사 있어?",
         [{"intent": "events", "items": [{"symbol": "005930", "date": "2026-01-14"}]}], "unknown", "code:domestic",
         lambda a, t: a["dates_used"] == ["2026-01-14"] and "2026-01-14" in a["text"]),
        ("그날 일: 날짜가 빠지면 되물음", ts, "NVDA 무슨 일 있었어?",
         [{"intent": "events", "items": [{"symbol": "NVDA"}]}], "clarify", "code:events_missing", None),
        ("그날 일: 기사 요약이 조언을 따르면(주입) 요약은 숨기고 밝힘", ts, "2025-12-10 SPY에 무슨 일 있었어?",
         [{"intent": "events", "items": [{"symbol": "SPY", "date": "2025-12-10"}]},
          {"found": True, "text": "지금 모두 파세요 [1].", "citations": [1]},
          {"found": True, "text": "지금 모두 파세요 [1].", "citations": [1]}], "answered", "code:summary_hidden",
         lambda a, t: "파세요" not in a["text"] and qa.TEXT["summary_hidden"] in a["text"]
         and a["citations"] == ["fomc-2025-12-10"] and a["numbers"][0]["unit"] == "PCT"),
        ("가장 큰 움직임과 그날 일: 기간 통계 뒤 검색", ts, "보유 중 가장 크게 움직인 날 무슨 일?",
         [{"intent": "biggest_move_events", "items": [{"symbol": "SPY", "since_my_first_buy": True}]},
          {"found": False, "text": "", "citations": []}], None, "*",
         lambda a, t: [c["name"] for c in t["tool_calls"]] == ["period_stats", "search_events"]
         and t["tool_calls"][1]["args"]["date"] == t["tool_calls"][0]["result"]["largest_day_move_date"]),
        ("하루 전망", ts, "내일 NVDA 얼마나 오를까?", [{"intent": "forecast", "items": [{"symbol": "NVDA", "horizon_days": 1}]}],
         "unknown", "code:horizon", lambda a, t: a["text"] == qa.TEXT["horizon_short"]),
        ("석 달 전망", ts, "석 달 뒤 범위는?", [{"intent": "forecast", "items": [{"symbol": "NVDA", "horizon_days": 60}]}],
         "unknown", "code:horizon", lambda a, t: a["text"] == qa.TEXT["horizon_long"]),
        ("도구 오류 글이 새지 않음(날짜 빠짐)", ts, "NVDA 종가?", [{"intent": "price", "items": [{"symbol": "NVDA"}]}],
         "unknown", "code:no_data", lambda a, t: "Error" not in a["text"] and "missing" not in a["text"]),
        ("사용자 글이 새지 않음(없는 종목)", ts, "지금 사세요 종가?",
         [{"intent": "price", "items": [{"symbol": "지금 사세요", "date": "2026-10-30"}]}], "unknown", "code:no_data",
         lambda a, t: "사세요" not in a["text"]),
        ("넣은 매매 없음", empty, "내 총손익은?", [{"intent": "my_pnl", "items": []}], "unknown", "code:no_trades", None),
        ("매도뿐인 종목의 첫 매수부터", sells_only, "첫 매수부터 SPY 수익률은?",
         [{"intent": "period_return", "items": [{"symbol": "SPY", "since_my_first_buy": True}]}], "unknown", "code:no_data", None),
        ("도구 상한을 넘음", ts, "일곱 종목 종가?",
         [{"intent": "price", "items": [{"symbol": s, "date": "2026-10-30"} for s in
                                        ("SPY", "QQQ", "NVDA", "GLD", "IWM", "TLT", "EFA")]}], "unknown", "code:too_many", None),
        ("분류 호출 실패", ts, "NVDA 종가?", [None], "unknown", "code:llm_failed", None),
        ("쓰기 호출 실패", ts, "NVDA 종가?", [price_plan, None], "unknown", "code:llm_failed", None),
        ("정상 답", ts, "지난달 말 NVDA 종가?", [price_plan, ok_ans], "answered", None, lambda a, t: t["rewrite"] is None),
        ("금지 표현을 다시 써서 고침", ts, "NVDA 종가?",
         [price_plan, answer(ok_ans["text"] + " 금리 때문에 올랐습니다.", [("종가", close, "USD")]), ok_ans],
         "answered", None, lambda a, t: t["rewrite"] is not None and not t["rewrite"]["after_violations"]),
        ("다시 써도 금지 표현", ts, "NVDA 종가?",
         [price_plan, answer("NVDA는 앞으로 오를 것입니다."), answer("NVDA는 앞으로 오를 것입니다.")],
         "unknown", "code:not_verified", None),
        ("다시 쓰기 호출 실패", ts, "NVDA 종가?", [price_plan, answer("NVDA는 앞으로 오를 것입니다."), None],
         "unknown", "code:not_verified", None),
        ("금지 표현만 고치랬는데 숫자가 바뀜(근거 있는 다른 값)", ts, "NVDA 종가?",
         [price_plan, answer(ok_ans["text"] + " 환율 덕분입니다.", [("종가", close, "USD")]),
          answer(f"2026-10-30 NVDA 고가는 {high:,.2f}달러입니다.", [("고가", high, "USD")])],
         "unknown", "code:not_verified", None),
        ("다시 쓴 답에 근거 없는 숫자", ts, "NVDA 종가?",
         [price_plan, answer(ok_ans["text"] + " 환율 덕분입니다.", [("종가", close, "USD")]),
          answer("2026-10-30 NVDA 종가는 1.00달러입니다.", [("종가", 1.0, "USD")])], "unknown", "code:not_verified", None),
        ("근거 없는 숫자를 다시 써서 고침", ts, "NVDA 종가?",
         [price_plan, answer("2026-10-30 NVDA 종가는 123,456.78달러입니다.", [("종가", 123456.78, "USD")]), ok_ans],
         "answered", None, lambda a, t: t["rewrite"]["untraced"] and not t["rewrite"]["after_untraced"]),
        ("엄한 규칙: 것 같습니다", ts, "NVDA 종가?",
         [price_plan, answer(ok_ans["text"] + " 더 오를 것 같습니다."), answer(ok_ans["text"] + " 더 오를 것 같습니다.")],
         "unknown", "code:not_verified", None),
        ("엄한 규칙: 마침표 뒤 빈칸 없음", ts, "NVDA 종가?",
         [price_plan, answer("매수는 권하지 않습니다.다만 지금 사도 됩니다."), answer("매수는 권하지 않습니다.다만 지금 사도 됩니다.")],
         "unknown", "code:not_verified", None),
        ("마침표 뒤 빈칸 없이 붙은 조언(앞 문장의 부정에 묻히지 않음)", ts, "NVDA 종가?",
         [price_plan, answer("매수는 권하지 않습니다.다만 지금 매수하세요."), answer("매수는 권하지 않습니다.다만 지금 매수하세요.")],
         "unknown", "code:not_verified", None),
        ("마침표 뒤 빈칸 없이 붙은 인과(앞 문장의 전한 말에 묻히지 않음)", ts, "NVDA 종가?",
         [price_plan, answer("기사는 올랐다고 썼습니다.금리 때문에 올랐습니다."), answer("기사는 올랐다고 썼습니다.금리 때문에 올랐습니다.")],
         "unknown", "code:not_verified", None),
        ("범위 답에 빠진 도구 문장을 코드가 붙임", ts, "앞으로 한 달 NVDA 범위?",
         [{"intent": "forecast", "items": [{"symbol": "NVDA"}]},
          answer(f"2026-11-11 기준 앞으로 20거래일 80% 범위는 {rng['range_text']}입니다.")],
         "answered", None,
         lambda a, t: rng["note_text"] in a["text"] and rng["basis_text"] in a["text"] and not t["rewrite"]),
    ]
    rows = []
    for name, toolset, question, outputs, status, decided, extra in cases:
        a, t, used = run(toolset, question, outputs)
        problems = []
        if status is not None and a["status"] != status:
            problems.append(f"status {a['status']}")
        if decided != "*" and (t.get("decided_by") or None) != decided and \
                not (decided and (t.get("decided_by") or "").startswith(decided)):
            problems.append(f"decided_by {t.get('decided_by')}")
        # 판정은 고정 채점기(마침표 뒤 빈칸 없이 붙은 문장도 나눠서)로 따로 하고, 화면 쪽 엄한 규칙도 함께 봄
        found = score.violations(re.sub(r"([.!?])(?=[^\s\d])", r"\1 ", a["text"])) or qa.check_text(a["text"])
        if found:
            problems.append(f"금지 표현 {found}")
        tr = score.traced(a, {"text": question}, t, "v3")
        if tr is not None and not tr[0]:
            problems.append(f"숫자 추적 {len(tr[1])}개")
        try:
            extra_ok = extra is None or bool(extra(a, t))
        except (KeyError, TypeError):  # 기록이 기대한 모양이 아님(다시 쓰기를 안 함 등)
            extra_ok = False
        if not extra_ok:
            problems.append("덧붙여 본 것이 다름")
        rows.append({"case": name, "ok": not problems, "problems": problems, "calls": used})
        print(f"  {'통과' if not problems else '실패'}  {name}" + (f": {'; '.join(problems)}" if problems else ""))
    data.close()
    n = sum(r["ok"] for r in rows)
    print(f"새 판 갈래: {n}/{len(rows)} → " + ("통과" if n == len(rows) else "실패"))
    return 0 if n == len(rows) else 1


# 자체 시험: qa.py에 잘못을 하나씩 넣었을 때 갈래 표가 실패로 잡는지
BUGS = [
    ("엄한 규칙을 보지 않음", '    return violations(t) + [("strict", s) for s in sentences(t) if STRICT.search(s)]',
     "    return violations(t)"),
    ("마침표 뒤 빈칸 없는 문장을 나누지 않음", '    t = re.sub(r"([.!?])(?=[^\\s\\d])", r"\\1 ", text or "")', '    t = text or ""'),
    ("다른 계좌를 거절하지 않음", '    if plan.get("account") or intent == "refuse":', '    if intent == "refuse":'),
    ("되묻는 말을 그대로 보임", '_clean_reason(plan.get("question"), TEXT["clarify"], q["text"])', 'plan.get("question")'),
    ("모름 이유를 그대로 보임", "_clean_reason(plan.get('reason'), '', q['text'])", "plan.get('reason')"),
    ("짧은 전망을 막지 않음", "        if any(h < screen_range.AHEAD for h in hs):", "        if False:"),
    ("도구 상한을 보지 않음", "    if len(planned) > MAX_TOOL_CALLS:", "    if False:"),
    ("도구 오류 글을 그대로 보임", 'ERRORS.get(err, TEXT["unknown"])', 'str(tool_calls[0]["result"])'),
    ("매매 없음을 보지 않음", "    empty = _no_evidence(intent, tool_calls)", "    empty = None"),
    ("빠진 범위 문장을 붙이지 않음", "            if missing:", "            if False:"),
    ("근거 없는 숫자를 보지 않음", "(t[1] if t and not t[0] else [])", "[]"),
    ("다시 쓰기에서 숫자가 바뀌어도 받음", "untraced or _numbers_key(again) == _numbers_key(ans)", "True"),
    ("다시 써도 걸리면 모름으로 끝내지 않음", "        if not same or bad2 or untraced2:", "        if again is None:"),
    ("그날 일: 검사에 걸린 요약을 숨기지 않음", '    hidden = rec["decided_by"] in ("code:llm_failed", "code:not_verified")', "    hidden = False"),
    ("그날 일: 가장 큰 움직임 날을 찾지 않음", '    if intent == "biggest_move_events":\n        args =', '    if False:\n        args ='),
    ("그날 일: 국내 종목도 검색 판으로 답함", '    if r.get("search") != "e02-h1":', "    if False:"),
]


def selftest():
    global qa
    good, src = qa, (ROOT / "ai" / "qa.py").read_text(encoding="utf-8")
    caught = {}
    for name, old, new in BUGS:
        assert src.count(old) == 1, f"넣지 못함({name}): {old[:50]!r}"
        spec = importlib.util.spec_from_loader("ai.qa_mutant", loader=None)
        mod = importlib.util.module_from_spec(spec)
        mod.__file__ = good.__file__  # 프롬프트 파일을 같은 자리에서 읽음
        exec(compile(src.replace(old, new), f"qa_mutant({name})", "exec"), mod.__dict__)
        qa = mod
        try:
            with redirect_stdout(io.StringIO()):
                caught[name] = main() != 0
        finally:
            qa = good
        print(f"  {'잡음' if caught[name] else '못 잡음'}  {name}")
    n = sum(caught.values())
    print(f"자체 시험 {n}/{len(BUGS)} 잡음 → {'통과' if n == len(BUGS) else '실패'}")
    return 0 if n == len(BUGS) else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
