#!/usr/bin/env python3
"""근거 질문(묶음 E)의 인용 정답(cite_any)을 만듭니다(설계서 4절 「묻고 답하기」: 근거 질문의 인용 정답은 7단계에 따로 만들어
따로 고정, lock.py --lock-cites).

사용(저장소 루트에서):
  .venv/bin/python eval/f_screen/make_cites.py --split dev     개발용만(시험). 판정 모델을 부름
  .venv/bin/python eval/f_screen/make_cites.py --split all     확인용까지(관련 표시 카파가 0.7을 넘은 뒤 한 번, 그다음 --lock-cites)

만드는 법(E02 3절 「관련 표시를 만드는 법」과 같음):
- 질문의 종목은 질문 글에서 종목 코드나 이름으로, 날짜는 정답 파일의 dates 첫 값으로 정합니다(코드, 모델 없음).
- 그날 검색 창의 기사에서 기준선과 H1의 상위 5건을 합친 풀을 판정 모델(ai/prompts/e02_judge_rel_v1.txt)이 표시하고,
  관련 기사와 같은 날 일정(전날 장 마감부터 그날 장 마감 전까지 발표)을 인용할 수 있는 번호로 둡니다.
- 국내 종목은 E02 1판 범위 밖이라 비웁니다(인용 채점을 하지 않음).
- 주입 시험의 가짜 기사는 넣지 않습니다(인용하면 안 되는 기사).

결과는 data/generated/f_screen/expected_cites.json(git 제외)입니다. 확인용 질문의 글과 결과는 화면에 찍지 않습니다.
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval" / "e02_rag"))

import duckdb  # noqa: E402
import judge as J  # noqa: E402  E02 관련 표시의 판정 모델(같은 기준과 같은 묶음 부르기)

from ai import news_search as NS  # noqa: E402
from ai import screen_data  # noqa: E402
from ai import tools as T  # noqa: E402

QUESTIONS = HERE / "questions.json"
EXPECTED = ROOT / "data" / "generated" / "f_screen" / "expected_questions.json"
OUT = ROOT / "data" / "generated" / "f_screen" / "expected_cites.json"


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def symbol_of(text, symbols):
    """질문 글의 종목: 종목 코드(대문자)나 이름이 들어 있는 것. 둘 이상이거나 없으면 None."""
    hits = {s for s, v in symbols.items() if s in text or v["name"] in text}
    return hits.pop() if len(hits) == 1 else None


def main():
    split = arg("--split", "dev")
    qs = [q for q in json.loads(QUESTIONS.read_text(encoding="utf-8")) if q["group"] == "E"]
    if split != "all":
        qs = [q for q in qs if q["split"] == split]
    exp = json.loads(EXPECTED.read_text(encoding="utf-8"))
    data = T.real_data()
    con = duckdb.connect(str(screen_data.eval_db()), read_only=True)
    days = [date.fromisoformat(x) for x in data.us_days]
    system = J.PROMPT.read_text(encoding="utf-8")
    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    shown = []
    try:
        for q in qs:
            sym = symbol_of(q["text"], data.symbols)
            d = (exp[q["id"]].get("dates") or [None])[0]
            if not sym or not d or (sym not in NS.COMPANIES and sym not in NS.TOPICS):
                out[q["id"]] = []
                shown.append((q, "비움(국내 종목이거나 종목, 날짜를 정하지 못함)"))
                continue
            t = date.fromisoformat(d)
            arts, (start, end) = NS.window_articles(con, days, t)
            pool = list(dict.fromkeys(a["id"] for v in NS.VERSIONS.values() for a in v(arts, sym)))
            by_id = {a["id"]: a for a in arts}
            item = {"id": f"cite-{q['id']}", "date": d, "symbol": sym, "window": [start.isoformat(), end.isoformat()]}
            _, labels, _, cost, _ = J.judge_one(item, [by_id[i] for i in pool], system)
            close = datetime.combine(t, datetime.min.time(), T.SW.ET) + timedelta(hours=13 if t in T.SW.HALF_DAYS else 16)
            events = [e["id"] for e in data.events_fn(d, start, close)]
            out[q["id"]] = [i for i in pool if labels[i]] + events
            shown.append((q, f"풀 {len(pool)}건 가운데 관련 {sum(labels.values())}건, 일정 {len(events)}건, ${cost:.4f}"))
    finally:
        con.close()
        data.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for q, msg in shown:
        print(f"  {q['id']}: {msg}" if q["split"] == "dev" else f"  (확인용 질문 하나 처리, 내용은 찍지 않음)")
    print(f"인용 정답 {len(out)}문항 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
