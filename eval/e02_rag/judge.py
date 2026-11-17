#!/usr/bin/env python3
"""E02 관련 표시의 판정 모델(docs/ai/experiments/E02-rag.md 3절 「관련 표시를 만드는 법」 2번).

풀(eval/e02_rag/pool_{split}.json)의 날마다 기사를 한 번에 묶어 판정 모델에 보내고, 기사마다 관련(true, false)을 받습니다.
판정 기준은 ai/prompts/e02_judge_rel_v1.txt이고, 사람 표본 탭에도 같은 기준을 한국어로 보입니다.

사용(저장소 루트에서):
  .venv/bin/python eval/e02_rag/judge.py [--ids E02-08] [--split dev]

- 표시(기사 번호와 true, false)는 eval/e02_rag/labels_{split}.json의 "judge"에 둡니다(저장소, 3절: 번호와 표시만).
- 판정 이유는 기사 내용을 옮길 수 있어 data/generated/e02/judge_{split}_reasons.json(git 제외)에 둡니다.
- 모델 호출은 ai/llm.py(kind="real", tag "e02-judge:{날짜 번호}")로, 장부는 data/generated/f_screen/ledger.jsonl입니다.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai import llm  # noqa: E402

HERE = Path(__file__).parent
GEN = ROOT / "data" / "generated" / "e02"
PROMPT = ROOT / "ai" / "prompts" / "e02_judge_rel_v1.txt"
NAMES = {"SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ (Nasdaq-100)", "IWM": "iShares Russell 2000 ETF",
         "SHY": "iShares 1-3 Year Treasury Bond ETF", "TLT": "iShares 20+ Year Treasury Bond ETF",
         "HYG": "iShares High Yield Corporate Bond ETF", "GLD": "SPDR Gold Shares", "USO": "United States Oil Fund",
         "UUP": "Invesco DB US Dollar Index Bullish Fund", "EFA": "iShares MSCI EAFE ETF",
         "EEM": "iShares MSCI Emerging Markets ETF", "IBIT": "iShares Bitcoin Trust", "NVDA": "NVIDIA", "AAPL": "Apple",
         "TSLA": "Tesla", "GOOGL": "Alphabet (Google)", "TSM": "TSMC"}
SCHEMA = {"type": "object", "required": ["labels"], "properties": {"labels": {"type": "array", "items": {
    "type": "object", "required": ["n", "relevant", "reason"],
    "properties": {"n": {"type": "integer"}, "relevant": {"type": "boolean"}, "reason": {"type": "string"}}}}}}
WORKERS = 3


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def prompt_for(item, arts):
    lines = [f"Trading day: {item['date']}. Symbol: {item['symbol']} ({NAMES[item['symbol']]}).",
             f"Search window (US Eastern): {item['window'][0]} to {item['window'][1]}.", "", "Articles:"]
    for n, a in enumerate(arts, 1):
        lines.append(f"[{n}] {a['time']} | {a['publisher']} | symbols: {', '.join(a['symbols'][:12]) or '-'}\n"
                     f"    title: {a['title']}\n    summary: {a['summary']}")
    return "\n".join(lines)


def judge_one(item, arts, system):
    r = llm.call(prompt_for(item, arts), system, schema=SCHEMA, tag=f"e02-judge:{item['id']}", kind="real")
    out = r["output"] or {}
    got = {x["n"]: x for x in out.get("labels", []) if isinstance(x, dict)}
    missing = [n for n in range(1, len(arts) + 1) if n not in got]
    if r["error"] or missing:
        raise RuntimeError(f"{item['id']}: 판정을 받지 못함({r['error'] or f'빠진 번호 {missing}'})")
    labels = {a["id"]: bool(got[n]["relevant"]) for n, a in enumerate(arts, 1)}
    reasons = {a["id"]: got[n]["reason"] for n, a in enumerate(arts, 1)}
    return item["id"], labels, reasons, r["cost_usd"] or 0, r["seconds"]


def main():
    split = arg("--split", "dev")
    pool = json.loads((HERE / f"pool_{split}.json").read_text(encoding="utf-8"))
    text = json.loads((GEN / f"pool_{split}_text.json").read_text(encoding="utf-8"))
    items = pool["items"]
    if arg("--ids"):
        want = arg("--ids").split(",")
        items = [it for it in items if it["id"] in want]
    system = PROMPT.read_text(encoding="utf-8")
    by_item = {it["id"]: {a["id"]: a for a in text[it["id"]]} for it in items}
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(lambda it: judge_one(it, [by_item[it["id"]][i] for i in it["pool"]], system), items))
    out_path = HERE / f"labels_{split}.json"
    labels = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {
        "note": "E02 관련 표시(기사 번호와 true, false). judge는 판정 모델, human은 사람 표본(진행판 탭에서 받아 옮김).",
        "judge_prompt": PROMPT.name, "judge_model": llm.MODEL, "judge": {}, "human": {}}
    reasons_path = GEN / f"judge_{split}_reasons.json"
    reasons = json.loads(reasons_path.read_text(encoding="utf-8")) if reasons_path.exists() else {}
    for iid, lab, why, _, _ in res:
        labels["judge"][iid] = lab
        reasons[iid] = why
    labels["judge"] = dict(sorted(labels["judge"].items()))
    out_path.write_text(json.dumps(labels, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    reasons_path.write_text(json.dumps(reasons, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    cost, calls = llm.spent()
    for iid, lab, _, c, sec in res:
        print(f"  {iid}: 관련 {sum(lab.values())}/{len(lab)}건, ${c:.4f}, {sec:.1f}초")
    print(f"판정 {len(res)}일, 관련 {sum(sum(l.values()) for _, l, *_ in res)}/{sum(len(l) for _, l, *_ in res)}건. "
          f"장부 누적 ${cost:.2f}, {calls}번")


if __name__ == "__main__":
    main()
