#!/usr/bin/env python3
"""E04 실행기: 질문 → 판(ai/agent.py) → 채점(score.py) → 결과 표와 기록.

사용:
  .venv/bin/python eval/e04_agent/run.py --variant baseline|h1 [--ids Q05,Q06] [--rerun]
  .venv/bin/python eval/e04_agent/run.py --variant h1 --split confirm --final
기본은 개발용 36개입니다. 확인용 24개는 --final을 줘야 돌고, 판마다 한 번만 돕니다(이미 기록이 있으면 멈춤).
--rerun은 개발용 가운데 시드로 고른 10개를 두 번 더 돌려 숫자와 status가 같은지 봅니다(모두 세 번).
정답(expected.json)은 채점에만 쓰고 모델에 넘기지 않습니다. 비용 상한은 ai/llm.py(BUDGET_USD)입니다.
기록: eval/e04_agent/runs/<판>_<나눔>_<시각>.jsonl(질문마다 답, 도구 호출, 채점)과 _summary.json.
"""
import hashlib
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai import agent, llm  # noqa: E402

import score  # noqa: E402

HERE = Path(__file__).parent
RUNS = HERE / "runs"
RERUN_SEED, RERUN_N = 20261112, 10
WORKERS = 3


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def one(variant, q):
    try:
        ans, trace = agent.VARIANTS[variant](q)
    except llm.BudgetExceeded:
        raise
    except Exception as e:  # 한 질문의 실패가 판 전체를 멈추지 않게
        ans, trace = agent._empty(f"{type(e).__name__}: {e}"), {"tool_calls": [], "data": [], "llm": [], "seconds": 0}
    r = score.score_one(q["id"], ans, trace)
    r.update(seconds=round(trace["seconds"], 2), cost_usd=sum(c["cost_usd"] or 0 for c in trace["llm"]),
             n_llm=len(trace["llm"]))
    return {"id": q["id"], "answer": ans, "trace": trace, "score": r}


def fmt(m, k):
    v = m[k]
    return f"{v[0]}/{v[1]}" if isinstance(v, tuple) else v


def main():
    variant, split = arg("--variant", "h1"), arg("--split", "dev")
    qs = [q for q in score.QUESTIONS.values() if q["split"] == split]
    if arg("--ids"):
        qs = [score.QUESTIONS[i] for i in arg("--ids").split(",")]
    if split == "confirm":
        if "--final" not in sys.argv or arg("--ids"):
            sys.exit("확인용은 --final로 전체를 한 번만 돌립니다")
        if list(RUNS.glob(f"{variant}_confirm_*.jsonl")):
            sys.exit(f"{variant}의 확인용 기록이 이미 있습니다. 다시 돌리지 않습니다")
    RUNS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{variant}_{split}{'_part' if arg('--ids') else ''}_{stamp}"
    boot, _ = llm.boot_seconds()
    with ThreadPoolExecutor(WORKERS) as pool:
        out = list(pool.map(lambda q: one(variant, q), qs))
    rerun = None
    if "--rerun" in sys.argv:
        ids = random.Random(RERUN_SEED).sample(sorted(q["id"] for q in score.QUESTIONS.values() if q["split"] == "dev"),
                                               RERUN_N)
        base = {o["id"]: o for o in out}
        with ThreadPoolExecutor(WORKERS) as pool:
            extra = list(pool.map(lambda i: one(variant, score.QUESTIONS[i]), ids * 2))
        # 같은 숫자를 두 번 적은 것은 하나로 셉니다(단위와 값의 집합으로 비교, 11/12 개발용 Q14에서 정함).
        key = lambda o: (o["answer"].get("status"),
                         sorted({(n["unit"], round(float(n["value"]), 6)) for n in o["answer"].get("numbers", [])}))
        diff = [i for i in ids if i in base and any(key(x) != key(base[i]) for x in extra if x["id"] == i)]
        rerun = {"ids": ids, "runs": 3, "same": len(ids) - len(diff), "differ": diff}
        with (RUNS / f"{name}_rerun.jsonl").open("w", encoding="utf-8") as f:
            for o in extra:
                f.write(json.dumps(o, ensure_ascii=False, default=str) + "\n")
    rows = [o["score"] for o in out]
    m = score.summarize(rows, boot)
    if rerun:
        m["pass"]["rerun==all"] = not rerun["differ"]
    with (RUNS / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False, default=str) + "\n")
    cost, calls = llm.spent()
    code = sorted([*ROOT.glob("ai/*.py"), *ROOT.glob("ai/prompts/*.txt"), HERE / "score.py", HERE / "run.py"])
    summary = {"variant": variant, "split": split, "questions": len(qs), "boot_s": boot, "metrics": m,
               "code_sha256": {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()[:16] for f in code},
               "rerun": rerun, "ledger_total_usd": round(cost, 4), "ledger_calls": calls}
    (RUNS / f"{name}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str) + "\n",
                                               encoding="utf-8")
    print(f"| 판 | {variant} ({split}, {len(qs)}개) |\n|---|---|")
    for k, label in [("numbers", "숫자 정답"), ("wrong_numbers", "틀린 숫자"), ("answered", "답한 비율"),
                     ("traced", "숫자 추적"), ("tools", "도구 선택"), ("dates", "날짜 밝힘"), ("warnings", "경고 전달"),
                     ("refused", "거절"), ("unknown_clarify", "모름과 되묻기"), ("citations", "인용"),
                     ("forbidden", "금지 표현"), ("injection", "주입 문장"), ("forecast", "전망 문구")]:
        print(f"| {label} | {fmt(m, k)} |")
    print(f"| p50, p95 | {m['p50_s']:.1f}초, {m['p95_s']:.1f}초(기동 {boot:.1f}초 x 호출 수를 뺌) |")
    print(f"| 질문당 비용 | ${m['cost_per_q']:.4f} |")
    if rerun:
        print(f"| 다시 실행 | {rerun['same']}/{len(rerun['ids'])} 같음 {rerun['differ'] or ''} |")
    print(f"\n통과선에 못 미친 것: {[k for k, v in m['pass'].items() if not v] or '없음'}")
    print("틀린 질문:", ", ".join(f"{r['id']}({','.join(k for k in ('status_ok', 'numbers_ok', 'traced', 'tools_ok', 'dates_ok', 'warnings_ok', 'citations_ok', 'injection_ok') if r.get(k) is False)}{',금지' if r['violations'] else ''})"
                                 for r in rows if any(r.get(k) is False for k in ('status_ok', 'numbers_ok', 'traced', 'tools_ok', 'dates_ok', 'warnings_ok', 'citations_ok', 'injection_ok')) or r['violations']) or "없음")
    print(f"기록: {(RUNS / name).relative_to(ROOT)}.jsonl, 장부 누적 ${cost:.2f}, {calls}번")


if __name__ == "__main__":
    main()
