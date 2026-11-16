#!/usr/bin/env python3
"""F 화면 평가 기준과 정답의 sha256을 고정하고 대조합니다(docs/ai/F-screen.md 2절 「실제 시세 보호」, 4절).

사용: .venv/bin/python eval/f_screen/lock.py --lock         평가 기준 확정 때 한 번
      .venv/bin/python eval/f_screen/lock.py --lock-cites   인용 정답을 7단계에 한 번
      .venv/bin/python eval/f_screen/lock.py --verify       그 뒤 언제든
      .venv/bin/python eval/f_screen/lock.py --relock "이유"  변경 기록을 적은 뒤 다시 고정

- 정답 파일과 원본은 실제 시세가 들어 있어 git에 넣지 않습니다(data/generated/, data/raw/). 저장소에는 그 지문만 남겨,
  결과를 본 뒤 정답을 바꾸지 않았음을 보입니다. E03의 criteria_lock.json과 같은 방식입니다.
- 문서는 해당 절만 셉니다(F-screen.md 2절 만들 것과 규칙, 4절 평가 기준, 6절 정한 것, E02-rag.md 3절, E04-agent.md 3절).
  절은 '## N. '부터 다음 '## ' 앞까지의 UTF-8 바이트입니다. 1절(문제), 3절(데이터 기록), 5절(일정), 7절(개발), 8절(결과)은
  일하며 바뀌는 곳이라 넣지 않습니다.
- 2절의 구조 설명처럼 평가와 무관하게 바꿔야 하면, F-screen.md 4절 「평가 기준 변경 기록」에 날짜와 이유를 적고
  `--relock "이유"`로 다시 고정합니다. 앞의 고정 기록은 history에 그대로 남습니다.
- 근거 질문의 인용 정답(expected_cites.json)은 E02 관련 표시(7단계) 뒤에 만들어 `--lock-cites`로 따로 고정합니다.
"""
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
LOCK = HERE / "lock.json"

CRITERIA = [
    ("docs/ai/F-screen.md", "2"),
    ("docs/ai/F-screen.md", "4"),
    ("docs/ai/F-screen.md", "6"),
    ("docs/ai/experiments/E02-rag.md", "3"),
    ("docs/ai/experiments/E04-agent.md", "3"),
    ("loaders/instruments.csv", None),
    ("eval/f_screen/warnings.json", None),
    ("eval/e02_rag/step0_survey.py", None),
    ("eval/step0_util.py", None),
    ("eval/e02_rag/search_window.py", None),
    ("eval/f_screen/cases_real.json", None),
    ("eval/f_screen/account.json", None),
    ("eval/f_screen/questions.json", None),
    ("eval/f_screen/real_world.py", None),
    ("eval/f_screen/make_expected_real.py", None),
    ("eval/f_screen/make_questions_real.py", None),
    ("eval/f_screen/range_expected.py", None),
    ("eval/f_screen/check_raw.py", None),
    ("eval/e02_rag/eval_dates.json", None),
    ("eval/e02_rag/pick_dates.py", None),
    ("eval/e01_pnl/make_expected.py", None),
    ("eval/e04_agent/score.py", None),
]
ANSWERS = [
    "data/generated/f_screen/expected_real.json",
    "data/generated/f_screen/expected_questions.json",
    "data/generated/f_screen/expected_range.json",
]
RAW_GLOBS = [
    "data/raw/daily_prices/*_20241113_20261112*.json",
    "data/raw/fx_rates/USDKRW_20241113_20261112.json",
    "data/raw/news_massive/ALL_20241113_20261112.json",
]


def section(text, num):
    start = text.index(f"\n## {num}. ") + 1
    nxt = text.find("\n## ", start + 4)
    return text[start:nxt + 1 if nxt >= 0 else len(text)]


def digest(rel, sec=None):
    path = ROOT / rel
    data = path.read_bytes()
    if sec:
        data = section(data.decode("utf-8"), sec).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def current():
    crit = {f"{p} {s}절" if s else p: digest(p, s) for p, s in CRITERIA}
    ans = {p: digest(p) for p in ANSWERS}
    raw = {}
    for g in RAW_GLOBS:
        for path in sorted(ROOT.glob(g)):
            raw[str(path.relative_to(ROOT))] = digest(str(path.relative_to(ROOT)))
    return crit, ans, raw


def lock(reason=None):
    history = []
    if LOCK.exists():
        if not reason:
            sys.exit("이미 고정했습니다. 바꾸려면 F-screen.md 4절 「평가 기준 변경 기록」에 날짜와 이유를 적은 뒤 --relock \"이유\"")
        old = json.loads(LOCK.read_text(encoding="utf-8"))
        history = old.pop("history", []) + [old]
    crit, ans, raw = current()
    rec = {"locked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "relock_reason": reason,
           "why": "F 화면 평가 기준과 정답을 개발 전에 고정(결정 0009). 정답과 원본은 git 제외라 지문만 둠",
           "criteria_sha256": crit, "answers_sha256": ans, "raw_inputs_sha256": raw,
           "check": "python3 eval/f_screen/lock.py --verify", "history": history}
    LOCK.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"고정함: 기준 {len(crit)}개, 정답 {len(ans)}개, 원본 {len(raw)}개 → {LOCK.relative_to(ROOT)}")


CITES = "data/generated/f_screen/expected_cites.json"


def lock_cites():
    rec = json.loads(LOCK.read_text(encoding="utf-8"))
    if "cites_sha256" in rec:
        sys.exit("인용 정답은 이미 고정했습니다")
    rec["cites_sha256"] = {CITES: digest(CITES)}
    rec["cites_locked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    LOCK.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"인용 정답을 고정함 → {LOCK.relative_to(ROOT)}")


def verify():
    rec = json.loads(LOCK.read_text(encoding="utf-8"))
    crit, ans, raw = current()
    out = 0
    groups = [("평가 기준", rec["criteria_sha256"], crit), ("정답", rec["answers_sha256"], ans),
              ("원본", rec["raw_inputs_sha256"], raw)]
    if "cites_sha256" in rec:
        groups.append(("인용 정답", rec["cites_sha256"], {CITES: digest(CITES)}))
    for name, want, got in groups:
        bad = [k for k, v in want.items() if got.get(k) != v]
        print(f"{name} {len(want) - len(bad)}/{len(want)} 같음" + (f", 바뀜: {bad}" if bad else ""))
        out |= bool(bad)
    return out


if __name__ == "__main__":
    if "--lock" in sys.argv:
        lock()
    elif "--relock" in sys.argv:
        i = sys.argv.index("--relock")
        lock(sys.argv[i + 1] if i + 1 < len(sys.argv) and sys.argv[i + 1].strip() else sys.exit("--relock 뒤에 이유를 씁니다"))
    elif "--lock-cites" in sys.argv:
        lock_cites()
    elif "--verify" in sys.argv:
        sys.exit(verify())
    else:
        sys.exit(__doc__)
