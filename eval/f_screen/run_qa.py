#!/usr/bin/env python3
"""F 화면 묻고 답하기(9번 칸) 실행기: 질문 → 판(ai/qa.py) → 채점(eval/e04_agent/score.py) → 결과 표와 기록.

사용(저장소 루트에서):
  .venv/bin/python eval/f_screen/run_qa.py [--ids F03,F16] [--rerun]
  .venv/bin/python eval/f_screen/run_qa.py --with-search      근거 질문(묶음 E)까지(검색 도구를 붙인 뒤, 7단계)
  .venv/bin/python eval/f_screen/run_qa.py --split confirm --final   확인용 16개를 한 번만(7단계 뒤)

- 질문은 eval/f_screen/questions.json, 정답은 data/generated/f_screen/expected_questions.json(git 제외, 채점에만 씀),
  계좌는 eval/f_screen/account.json(화면에 넣은 매매로 봄)입니다. 주입 문장 질문은 F25, F26입니다(설계서 4절).
- 기본은 개발용 20개 가운데 근거 질문(묶음 E: F25, F28)을 뺀 18개입니다. 설계서 4절 「묻고 답하기」: 근거 질문은 검색
  도구를 붙인 뒤 돌립니다. 그 전의 판은 그날 일을 묻는 질문에 모른다고 답합니다.
- --rerun: 설계서 4절대로 개발용 가운데 번호 순으로 숫자 정답이 있는 앞 10개를 두 번 더 돌려(모두 세 번) 숫자와 status가
  같은지 봅니다. 같은 숫자를 두 번 적은 것은 하나로 셉니다(E04 판 3의 비교 정의).
- 숫자 추적은 채점기 v3(설계서 4절)입니다. 인용 정답(expected_cites.json)은 7단계에 만들어, 그 전에는 인용 채점을 건너뜁니다.
- 기록과 장부는 실제 데이터 자리(data/generated/f_screen/runs/, ledger.jsonl, git 제외)입니다.
"""
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval" / "e04_agent"))

from ai import agent, llm, qa  # noqa: E402
from ai import tools as T  # noqa: E402

import score  # noqa: E402

HERE = Path(__file__).parent
QUESTIONS = HERE / "questions.json"
EXPECTED = ROOT / "data" / "generated" / "f_screen" / "expected_questions.json"
CITES = ROOT / "data" / "generated" / "f_screen" / "expected_cites.json"
ACCOUNT = HERE / "account.json"
INJECTION = ["F25", "F26"]
RERUN = ["F03", "F04", "F05", "F06", "F09", "F11", "F12", "F16", "F17", "F19"]  # 설계서 4절
TRACE = "v3"
WORKERS = 3
VARIANT = "f1"


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def one(data, account, suite, q):
    try:
        ans, trace = qa.ask(T.Toolset(data, q["today"], account), q)
    except llm.BudgetExceeded:
        raise
    except Exception as e:  # 한 질문의 실패가 판 전체를 멈추지 않게
        ans = agent._empty(f"{type(e).__name__}: {e}")
        trace = {"tool_calls": [], "data": [], "llm": [], "seconds": 0}
    r = score.score_one(q["id"], ans, trace, trace_ver=TRACE, suite=suite)
    r.update(seconds=round(trace["seconds"], 2), cost_usd=sum(c["cost_usd"] or 0 for c in trace["llm"]),
             n_llm=len(trace["llm"]), decided_by=trace.get("decided_by"), rewrite=bool(trace.get("rewrite")))
    return {"id": q["id"], "answer": ans, "trace": trace, "score": r}


def main():
    split = arg("--split", "dev")
    suite = score.load_suite(QUESTIONS, EXPECTED, INJECTION, CITES)
    qs = [q for q in suite["questions"].values() if q["split"] == split]
    if "--with-search" not in sys.argv:
        qs = [q for q in qs if q["group"] != "E"]
    if arg("--ids"):
        qs = [suite["questions"][i] for i in arg("--ids").split(",")]
        if any(q["split"] != split for q in qs):  # 확인용 번호를 개발용 기록으로 돌려 「한 번만」을 우회하지 못하게
            sys.exit(f"--ids의 질문은 모두 --split {split}이어야 합니다")
    runs = llm.runs_dir("real")
    if split == "confirm":
        if "--final" not in sys.argv or arg("--ids") or "--with-search" not in sys.argv:
            sys.exit("확인용은 검색 도구를 붙인 뒤 --with-search --final로 전체를 한 번만 돌립니다")
        if list(runs.glob(f"{VARIANT}_confirm_*.jsonl")):
            sys.exit(f"{VARIANT}의 확인용 기록이 이미 있습니다. 다시 돌리지 않습니다")
    runs.mkdir(parents=True, exist_ok=True)
    acct = json.loads(ACCOUNT.read_text(encoding="utf-8"))
    account = {"id": "screen", "trades": acct["trades"]}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{VARIANT}_{split}{'_part' if arg('--ids') else ''}_{stamp}"
    data = T.real_data()
    try:
        boot, _ = llm.boot_seconds(kind="real")
        with ThreadPoolExecutor(WORKERS) as pool:
            out = list(pool.map(lambda q: one(data, account, suite, q), qs))
        rerun = None
        if "--rerun" in sys.argv:
            ids = [i for i in RERUN if any(q["id"] == i for q in qs)]
            base = {o["id"]: o for o in out}
            with ThreadPoolExecutor(WORKERS) as pool:
                extra = list(pool.map(lambda i: one(data, account, suite, suite["questions"][i]), ids * 2))
            key = lambda o: (o["answer"].get("status"),
                             sorted({(n["unit"], round(float(n["value"]), 6)) for n in o["answer"].get("numbers", [])}))
            diff = [i for i in ids if i in base and any(key(x) != key(base[i]) for x in extra if x["id"] == i)]
            rerun = {"ids": ids, "runs": 3, "same": len(ids) - len(diff), "differ": diff}
            with (runs / f"{name}_rerun.jsonl").open("w", encoding="utf-8") as f:
                for o in extra:
                    f.write(json.dumps(o, ensure_ascii=False, default=str) + "\n")
    finally:
        data.close()
    rows = [o["score"] for o in out]
    m = score.summarize(rows, boot)
    if rerun:
        m["pass"]["rerun==all"] = not rerun["differ"]
    with (runs / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False, default=str) + "\n")
    cost, calls = llm.spent()
    code = sorted([*ROOT.glob("ai/*.py"), *ROOT.glob("ai/prompts/f1_*.txt"), ROOT / "eval/e04_agent/score.py",
                   Path(__file__)])
    summary = {"variant": VARIANT, "split": split, "questions": [q["id"] for q in qs], "boot_s": boot, "metrics": m,
               "trace": TRACE, "code_sha256": {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()[:16]
                                               for f in code},
               "rerun": rerun, "ledger_total_usd": round(cost, 4), "ledger_calls": calls}
    (runs / f"{name}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str) + "\n",
                                               encoding="utf-8")
    fmt = lambda k: (f"{m[k][0]}/{m[k][1]}" if isinstance(m[k], tuple) else m[k])
    print(f"| 판 | {VARIANT} ({split}, {len(qs)}개) |\n|---|---|")
    for k, label in [("numbers", "숫자 정답"), ("wrong_numbers", "틀린 숫자"), ("answered", "답한 비율"),
                     ("traced", "숫자 추적"), ("tools", "도구 선택"), ("dates", "날짜 밝힘"), ("warnings", "경고 전달"),
                     ("refused", "거절"), ("unknown_clarify", "모름과 되묻기"), ("citations", "인용"),
                     ("forbidden", "금지 표현"), ("injection", "주입 문장"), ("forecast", "전망 문구")]:
        print(f"| {label} | {fmt(k)} |")
    print(f"| p50, p95 | {m['p50_s']:.1f}초, {m['p95_s']:.1f}초(기동 {boot:.1f}초 x 호출 수를 뺌) |")
    print(f"| 질문당 비용 | ${m['cost_per_q']:.4f} |")
    if rerun:
        print(f"| 다시 실행 | {rerun['same']}/{len(rerun['ids'])} 같음 {rerun['differ'] or ''} |")
    print(f"| 코드가 정한 답 | {sum(1 for r in rows if r.get('decided_by'))}개, 다시 쓰기 {sum(r.get('rewrite', False) for r in rows)}번 |")
    print(f"\n통과선에 못 미친 것: {[k for k, v in m['pass'].items() if not v] or '없음'}")
    keys = ("status_ok", "numbers_ok", "traced", "tools_ok", "dates_ok", "warnings_ok", "citations_ok", "injection_ok")
    bad = [r for r in rows if any(r.get(k) is False for k in keys) or r["violations"]]
    print("틀린 질문:", ", ".join(f"{r['id']}({','.join(k for k in keys if r.get(k) is False)}{',금지' if r['violations'] else ''})"
                                 for r in bad) or "없음")
    print(f"기록: {(runs / name).relative_to(ROOT)}.jsonl, 장부 누적 ${cost:.2f}, {calls}번")


if __name__ == "__main__":
    main()
