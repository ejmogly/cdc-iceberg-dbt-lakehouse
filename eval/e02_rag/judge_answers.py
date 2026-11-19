#!/usr/bin/env python3
"""E02 답의 근거 충실도와 인과 표현을 판정 모델로 셉니다(3절 「지표와 통과선」의 근거 충실도 95%, 인과 표현 0건).

사용(저장소 루트에서):
  .venv/bin/python eval/e02_rag/judge_answers.py [--run 기록이름]   기본은 개발용 마지막 전체 실행
  .venv/bin/python eval/e02_rag/judge_answers.py --sample          사람 표본 20문장을 고름(판정 뒤 한 번)
  .venv/bin/python eval/e02_rag/judge_answers.py --selftest        일부러 틀린 문장을 잡는지(모델 1번)

- 모델이 쓴 부분(코드의 움직임 문장 뒤)을 문장으로 나누고, 문장마다 인용한 기사의 제목과 요약을 함께 보냅니다.
  답 하나에 한 번 부릅니다. 기준은 ai/prompts/e02_judge_ans_v1.txt(3절 12문장 규칙과 같은 뜻).
- 저장소에는 답 번호, 문장 순번, 인용 번호, true와 false만 둡니다(eval/e02_rag/answer_judge_{split}.json).
  문장 글과 판정 이유는 data/generated/e02/(git 제외)입니다.
- 사람 표본(2026-11-17 담당자 결정, 20문장): 판정 모델이 「뒷받침 안 됨」이라 한 문장을 모두(8개까지) 넣고,
  나머지를 「뒷받침됨」 문장에서 시드로 골라 20개를 채웁니다. 판정 모델이 거의 다 「예」라고 하면 카파를 셀 수 없어서
  미리 정한 규칙입니다. 일치율은 이 고른 표본의 값이라 전체의 일치율과 다릅니다(결과에 밝힘).
"""
import json
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from ai import e02_answer as A  # noqa: E402
from ai import llm, screen_data  # noqa: E402

GEN = ROOT / "data" / "generated" / "e02"
RUNS = GEN / "runs"
PROMPT = ROOT / "ai" / "prompts" / "e02_judge_ans_v1.txt"
SCHEMA = {"type": "object", "required": ["sentences"], "properties": {"sentences": {"type": "array", "items": {
    "type": "object", "required": ["n", "supported", "causal", "reason"],
    "properties": {"n": {"type": "integer"}, "supported": {"type": "boolean"}, "causal": {"type": "boolean"},
                   "reason": {"type": "string"}}}}}}
SEED, N_SAMPLE, MAX_NO = 20261117, 20, 8
WORKERS = 3


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def sentences(model_text):
    """문장 목록 [(글, 인용 번호)]. 「…전했습니다. [4]」처럼 마침표 뒤에 따로 붙은 인용은 앞 문장에 붙입니다."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", model_text.strip()) if p.strip()]
    texts = []
    for p in parts:
        lead = re.match(r"^((?:\[\d+\]\s*)+)(.*)$", p, re.S)  # 「[1] 기사 [2]는…」의 앞 [1]은 앞 문장의 인용
        if texts and lead:
            texts[-1] += " " + lead.group(1).strip()
            p = lead.group(2).strip()
        if p and not re.fullmatch(r"\.?", p):
            texts.append(p)
    uniq = lambda xs: list(dict.fromkeys(xs))
    return [(t, uniq(int(n) for n in A.CITE.findall(t))) for t in texts]


def model_part(text):
    return text.split("(2배 초과가 큰 움직임).", 1)[-1].strip()


def latest_run(split):
    runs = sorted(RUNS.glob(f"e02w*_{split}_2*.jsonl"))  # 일부만 돌린 기록(_part)은 빼고 전체 실행만(판 이름 순, 같은 판은 시각 순)
    if not runs:
        sys.exit("판정할 실행 기록이 없습니다(run.py를 먼저)")
    return runs[-1]


def judge_one(rec, arts_by_id, system):
    it, ans, tr = rec["item"], rec["answer"], rec["trace"]
    sents = sentences(model_part(ans["text"]))
    by_n = {c["n"]: c["id"] for c in ans["citations"]}
    retrieved = tr["retrieved"]
    lines = [f"Trading day {it['date']}, symbol {it['symbol']}.", "", "Sentences:"]
    for k, (s, cites) in enumerate(sents, 1):
        lines.append(f"({k}) cites {cites}: {s}")
    lines += ["", "Cited articles:"]
    for n in sorted({n for _, c in sents for n in c}):
        aid = by_n.get(n) or (retrieved[n - 1] if 0 < n <= len(retrieved) else None)
        a = arts_by_id.get(aid, {})
        lines.append(f"[{n}] title: {a.get('title', '(none)')}\n    summary: {a.get('summary', '')}")
    r = llm.call("\n".join(lines), system, schema=SCHEMA, tag=f"e02-judge-ans:{it['id']}:{tr['version']}", kind="real")
    got = {x["n"]: x for x in (r["output"] or {}).get("sentences", []) if isinstance(x, dict)}
    missing = [k for k in range(1, len(sents) + 1) if k not in got]
    if r["error"] or missing:
        raise RuntimeError(f"{it['id']}:{tr['version']}: 판정을 받지 못함({r['error'] or missing})")
    key = f"{it['id']}:{tr['version']}"
    rows = [{"s": k, "cites": c, "supported": bool(got[k]["supported"]), "causal": bool(got[k]["causal"])}
            for k, (_, c) in enumerate(sents, 1)]
    detail = [{"s": k, "text": s, "cites": c, "reason": got[k]["reason"]} for k, (s, c) in enumerate(sents, 1)]
    return key, rows, detail, r["cost_usd"] or 0


def sample(split):
    """사람 표본 20문장(문장 글과 인용 기사)을 data/generated/e02/faith_sample_{split}.json에 둠."""
    j = json.loads((HERE / f"answer_judge_{split}.json").read_text(encoding="utf-8"))
    detail = json.loads((GEN / f"answer_judge_{split}_detail.json").read_text(encoding="utf-8"))
    recs = {f"{o['item']['id']}:{o['trace']['version']}": o for o in map(json.loads, open(ROOT / j["run"], encoding="utf-8"))}
    flat = [(key, r) for key, rows in j["items"].items() for r in rows]
    rng = random.Random(SEED)
    no = [x for x in flat if not x[1]["supported"]]
    rng.shuffle(no)
    yes = [x for x in flat if x[1]["supported"]]
    rng.shuffle(yes)
    pick = no[:MAX_NO] + yes[:N_SAMPLE - min(len(no), MAX_NO)]
    rng.shuffle(pick)
    con = duckdb.connect(str(screen_data.eval_db()), read_only=True)
    out = []
    for k, (key, r) in enumerate(pick, 1):
        rec = recs[key]
        by_n = {c["n"]: c["id"] for c in rec["answer"]["citations"]}
        ids = [by_n.get(n) or rec["trace"]["retrieved"][n - 1] for n in r["cites"]]
        arts = [dict(zip(("id", "title", "summary", "publisher"), con.execute(
            "select id, title, description, publisher from news where id = ?", [i]).fetchone())) for i in ids]
        text = next(d["text"] for d in detail[key] if d["s"] == r["s"])
        out.append({"key": f"S{k:02d}", "answer": key, "s": r["s"], "judge_supported": r["supported"], "date": rec["item"]["date"],
                    "symbol": rec["item"]["symbol"], "text": text, "cites": r["cites"], "articles": arts})
    con.close()
    (GEN / f"faith_sample_{split}.json").write_text(json.dumps({"seed": SEED, "rule": f"판정 「아니오」 모두(최대 {MAX_NO}) + 「예」에서 시드로 채움",
                                                                "judge_file": f"eval/e02_rag/answer_judge_{split}.json({j['run']})",
                                                                "judge_no_in_sample": min(len(no), MAX_NO), "items": out},
                                                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"사람 표본 {len(out)}문장(판정 「아니오」 {min(len(no), MAX_NO)}개 포함, 전체 문장 {len(flat)}개)")


def selftest(split):
    """판정 모델이 일부러 틀린 문장을 잡는지(너그러운지) 봅니다. 개발 답(TSLA 2026-04-02, H1)의 뒷받침된 문장을 바꿔
    숫자를 틀리게, 없는 사실을 더하게, 인과 표현을 넣게 만들고 원래 문장 둘과 섞어 한 번 부릅니다. 바꾼 문장은 git 제외 파일의
    문장으로 실행 때 만들어, 저장소에는 기사 글이 들어가지 않습니다."""
    j = json.loads((HERE / f"answer_judge_{split}.json").read_text(encoding="utf-8"))
    detail = json.loads((GEN / f"answer_judge_{split}_detail.json").read_text(encoding="utf-8"))
    key = "E02-16:h1"
    rec = next(o for o in map(json.loads, open(ROOT / j["run"], encoding="utf-8"))
               if f"{o['item']['id']}:{o['trace']['version']}" == key)
    sents = [(d["text"], d["cites"]) for d in detail[key]]
    first, c1 = sents[0]
    m = re.search(r"\d+(?:,\d{3})*(?:\.\d+)?%?", A.CITE.sub("", first))
    wrong_num = first.replace(m.group(0), m.group(0).replace(m.group(0).rstrip("%"), str(int(float(m.group(0).rstrip("%").replace(",", ""))) + 7)), 1)
    cases = [(first, c1, True, False), (sents[1][0], sents[1][1], True, False),
             (wrong_num, c1, False, False),
             (A.CITE.sub("", first).rstrip(". ") + f" 같은 날 테슬라는 멕시코 새 공장 착공도 발표했다고 전했습니다 [{c1[0]}].", c1, False, False),
             (f"인도량이 예상에 못 미친 탓에 테슬라 주가가 내렸습니다 [{c1[0]}].", c1, None, True),
             (f"기사 [{c1[0]}]는 인도량이 예상에 못 미쳤다고 썼고, 같은 날 주가도 내렸다고 전했습니다 [{c1[0]}].", c1, None, False)]
    fake = {"item": rec["item"], "answer": {"text": "(2배 초과가 큰 움직임). " + " ".join(t for t, *_ in cases),
                                            "citations": rec["answer"]["citations"]}, "trace": {**rec["trace"], "version": "selftest"}}
    con = duckdb.connect(str(screen_data.eval_db()), read_only=True)
    ids = rec["trace"]["retrieved"]
    arts = {i: {"title": t, "summary": d or ""} for i, t, d in con.execute(
        f"select id, title, description from news where id in ({','.join('?' * len(ids))})", ids).fetchall()}
    con.close()
    _, rows, _, cost = judge_one(fake, arts, PROMPT.read_text(encoding="utf-8"))
    names = ["원래 문장 1", "원래 문장 2", "숫자를 바꿈", "없는 사실을 더함", "인과 표현(탓에)", "출처를 밝혀 나란히 둠"]
    ok = []
    for name, (_, _, sup, cau), r in zip(names, cases, rows):
        good = (sup is None or r["supported"] == sup) and r["causal"] == cau
        ok.append(good)
        print(f"  {'맞음' if good else '틀림'}  {name}: supported {r['supported']}, causal {r['causal']}")
    print(f"판정 모델 자체 시험 {sum(ok)}/{len(ok)}(${cost:.3f}) → {'통과' if all(ok) else '실패'}")
    return 0 if all(ok) else 1


def main():
    split = arg("--split", "dev")
    if "--sample" in sys.argv:
        return sample(split)
    if "--selftest" in sys.argv:
        return selftest(split)
    run = Path(arg("--run")) if arg("--run") else latest_run(split)
    if not run.is_absolute():
        run = RUNS / (run.name if run.suffix else run.name + ".jsonl")
    recs = [json.loads(l) for l in open(run, encoding="utf-8")]
    todo = [r for r in recs if r["answer"]["status"] == "answered"]
    ids = {a for r in todo for a in r["trace"]["retrieved"]}
    con = duckdb.connect(str(screen_data.eval_db()), read_only=True)
    arts = {i: {"title": t, "summary": d or ""} for i, t, d in con.execute(
        f"select id, title, description from news where id in ({','.join('?' * len(ids))})", list(ids)).fetchall()} if ids else {}
    con.close()
    system = PROMPT.read_text(encoding="utf-8")
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(lambda r: judge_one(r, arts, system), todo))
    out = {"note": "E02 답 문장의 판정(근거 충실도, 인과 표현). 문장 글은 git 제외 파일.", "run": str(run.relative_to(ROOT)),
           "judge_prompt": PROMPT.name, "judge_model": llm.MODEL, "items": {k: rows for k, rows, _, _ in sorted(res)}}
    (HERE / f"answer_judge_{split}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    GEN.mkdir(parents=True, exist_ok=True)
    (GEN / f"answer_judge_{split}_detail.json").write_text(
        json.dumps({k: d for k, _, d, _ in res}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for v in sorted({k.split(":")[1] for k in out["items"]}):
        rows = [r for k, rs in out["items"].items() if k.endswith(":" + v) for r in rs]
        sup = sum(r["supported"] for r in rows)
        print(f"{v}: 답 {sum(k.endswith(':' + v) for k in out['items'])}개, 문장 {len(rows)}개, 근거 충실도 {sup}/{len(rows)}"
              f" = {sup / len(rows) * 100:.1f}%(통과선 95%), 인과 표현(판정 모델) {sum(r['causal'] for r in rows)}건")
    cost, calls = llm.spent()
    print(f"판정 {len(res)}번, ${sum(c for *_, c in res):.3f}. 장부 누적 ${cost:.2f}, {calls}번")


if __name__ == "__main__":
    main()
