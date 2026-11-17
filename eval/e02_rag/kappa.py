#!/usr/bin/env python3
"""E02 관련 표시: 사람 표본과 판정 모델의 일치도(코헨 카파). E02 3절 「관련 표시를 만드는 법」 4번(0.7 이상일 때만
판정 모델 점수를 씀).

사용(저장소 루트에서):
  .venv/bin/python eval/e02_rag/kappa.py --db 받은폴더   진행판 db(e02_labels)를 받아 labels_dev.json의 human에 옮기고 셈
  .venv/bin/python eval/e02_rag/kappa.py                      이미 옮긴 human으로 셈
  .venv/bin/python eval/e02_rag/kappa.py --selftest
  .venv/bin/python eval/e02_rag/kappa.py --faith --db 받은폴더   근거 충실도 표본(e02_faith, 20문장)과 판정 모델

- db 폴더는 Claude가 ArtifactData list(collection e02_labels, out_dir)로 받은 것. 파일 하나가 문서 하나이고
  내용은 {article, item, label, at}입니다(2026-11-17 checks 컬렉션으로 모양 확인).
- 결과는 eval/e02_rag/kappa_{split}.json(번호와 수만)에 둡니다.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
PASS = 0.7


def kappa(pairs):
    """pairs: [(판정, 사람)] true/false. (카파, 일치율, 표). 기대 일치가 1이면 카파는 None."""
    n = len(pairs)
    if not n:
        return None, None, {}
    table = {(j, h): sum(1 for a, b in pairs if (a, b) == (j, h)) for j in (True, False) for h in (True, False)}
    po = (table[True, True] + table[False, False]) / n
    jy, hy = (table[True, True] + table[True, False]) / n, (table[True, True] + table[False, True]) / n
    pe = jy * hy + (1 - jy) * (1 - hy)
    return (None if pe == 1 else (po - pe) / (1 - pe)), po, table


def import_db(path, labels):
    """ArtifactData가 out_dir로 저장한 문서 파일들(폴더 아래 *.json)을 human에 옮김. 같은 기사를 두 번 누른 것은 마지막 at."""
    files = sorted(Path(path).rglob("*.json"))
    if not files:
        sys.exit(f"{path} 아래에 문서 파일이 없습니다")
    human = {}
    for f in files:
        body = json.loads(f.read_text(encoding="utf-8"))
        item, art = body["item"], body["article"]
        cur = human.setdefault(item, {}).get(art)
        if cur is None or body.get("at", "") >= cur[1]:
            human[item][art] = (bool(body["label"]), body.get("at", ""))
    labels["human"] = {i: {a: v[0] for a, v in sorted(arts.items())} for i, arts in sorted(human.items())}
    return labels


def selftest():
    cases = [([(True, True)] * 20 + [(False, False)] * 60, 1.0),  # 모두 같음
             ([(True, False)] * 20 + [(False, True)] * 60, None),   # 계산은 되지만 값은 음수, 아래에서 따로 봄
             ([(True, True)] * 10 + [(True, False)] * 5 + [(False, True)] * 5 + [(False, False)] * 60, None)]
    k1, *_ = kappa(cases[0][0])
    k2, *_ = kappa(cases[1][0])
    k3, po3, _ = kappa(cases[2][0])
    # 셋째: po = 70/80 = 0.875, pe = (15/80)^2 + (65/80)^2 = 0.03515625 + 0.66015625 = 0.6953125, k = 0.589744
    ok = [k1 == 1.0, k2 is not None and k2 < 0, abs(po3 - 0.875) < 1e-12 and abs(k3 - (0.875 - 0.6953125) / (1 - 0.6953125)) < 1e-12,
          kappa([(True, True)] * 5)[0] is None]
    print(f"카파 자체 시험 {sum(ok)}/{len(ok)} → {'통과' if all(ok) else '실패'}")
    return 0 if all(ok) else 1


def faith(split, db):
    """근거 충실도 표본: 사람(e02_faith)과 판정 모델(answer_judge_{split}.json의 supported). 표본은 판정 「아니오」를 모두 넣고
    「예」로 채운 것이라(judge_answers.py), 판정이 모두 「예」면 카파를 셀 수 없어 일치율(사람도 「예」라 한 비율)을 봅니다."""
    judge = json.loads((HERE / f"answer_judge_{split}.json").read_text(encoding="utf-8"))["items"]
    pairs, rows = [], []
    for f in sorted(Path(db).rglob("*.json")):
        body = json.loads(f.read_text(encoding="utf-8"))
        j = next(r["supported"] for r in judge[body["answer"]] if r["s"] == body["s"])
        pairs.append((j, bool(body["label"])))
        rows.append({"key": f.stem, "answer": body["answer"], "s": body["s"], "judge": j, "human": bool(body["label"])})
    k, po, t = kappa(pairs)
    if len({j for j, _ in pairs}) < 2 or len({h for _, h in pairs}) < 2:
        k = None  # 한쪽이 모두 같은 값이면 카파는 0이나 1로 떨어져 뜻이 없음(일치율로 봄)
    human_yes = sum(h for _, h in pairs)
    out = {"split": split, "pairs": len(pairs), "kappa": None if k is None else round(k, 4),
           "agreement": None if po is None else round(po, 4), "human_supported": human_yes,
           "judge_supported": sum(j for j, _ in pairs), "rows": rows}
    (HERE / f"faith_human_{split}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"충실도 표본 {len(pairs)}문장: 사람 「예」 {human_yes}, 판정 「예」 {out['judge_supported']}, 일치 {out['agreement']}, "
          f"카파 {out['kappa'] if k is not None else '뜻이 없음(한쪽이 모두 같은 값이라 일치율로 봄)'}")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--faith" in sys.argv:
        return faith(sys.argv[sys.argv.index("--split") + 1] if "--split" in sys.argv else "dev",
                     sys.argv[sys.argv.index("--db") + 1])
    split = sys.argv[sys.argv.index("--split") + 1] if "--split" in sys.argv else "dev"
    path = HERE / f"labels_{split}.json"
    labels = json.loads(path.read_text(encoding="utf-8"))
    if "--db" in sys.argv:
        labels = import_db(sys.argv[sys.argv.index("--db") + 1], labels)
        path.write_text(json.dumps(labels, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    judge, human = labels["judge"], labels.get("human", {})
    pairs, per_item, missing = [], {}, 0
    for item, arts in judge.items():
        for art, j in arts.items():
            h = human.get(item, {}).get(art)
            if h is None:
                missing += 1
                continue
            pairs.append((j, h))
            per_item.setdefault(item, []).append((j, h))
    k, po, t = kappa(pairs)
    out = {"split": split, "pairs": len(pairs), "missing_human": missing, "kappa": None if k is None else round(k, 4),
           "agreement": None if po is None else round(po, 4), "pass_line": PASS,
           "pass": k is not None and k >= PASS and missing == 0,
           "table": {"judge_yes_human_yes": t.get((True, True)), "judge_yes_human_no": t.get((True, False)),
                     "judge_no_human_yes": t.get((False, True)), "judge_no_human_no": t.get((False, False))},
           "disagree_by_item": {i: sum(a != b for a, b in v) for i, v in sorted(per_item.items()) if any(a != b for a, b in v)}}
    (HERE / f"kappa_{split}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"사람 표본 {len(pairs)}건(빠짐 {missing}), 일치 {out['agreement']}, 카파 {out['kappa']} "
          f"→ {'통과' if out['pass'] else '통과선(0.7) 못 미침 또는 빠짐 있음'}")
    print(f"표(판정/사람): 예/예 {out['table']['judge_yes_human_yes']}, 예/아니오 {out['table']['judge_yes_human_no']}, "
          f"아니오/예 {out['table']['judge_no_human_yes']}, 아니오/아니오 {out['table']['judge_no_human_no']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
