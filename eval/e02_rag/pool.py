#!/usr/bin/env python3
"""E02 관련 표시의 풀링(docs/ai/experiments/E02-rag.md 3절 「관련 표시를 만드는 법」 1번).

큰 움직임 날마다 기준선과 각 판(지금은 H1)의 상위 5건을 합친 기사만 표시 대상으로 모읍니다. recall@5는 이 풀
안에서 세므로 판끼리 비교하는 값입니다.

사용(저장소 루트에서):
  .venv/bin/python eval/e02_rag/pool.py                개발용 큰 움직임 날(14개)
  .venv/bin/python eval/e02_rag/pool.py --split confirm   확인용(마지막 판을 정한 뒤 한 번)

- 저장소에는 날짜, 종목, 기사 번호, 판마다의 순위만 둡니다(eval/e02_rag/pool_{split}.json, 3절 재배포 금지).
- 표시할 때 볼 제목과 요약은 data/generated/e02/pool_{split}_text.json(git 제외)에 둡니다.
- 근거를 뺀 날과 평범한 날은 recall을 세지 않아 풀링하지 않습니다(답만 봄).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from ai import news_search as NS  # noqa: E402
from ai import screen_data  # noqa: E402

HERE = Path(__file__).parent
DATES = HERE / "eval_dates.json"
GEN = ROOT / "data" / "generated" / "e02"


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    split = arg("--split", "dev")
    items = [r for r in json.loads(DATES.read_text(encoding="utf-8"))["items"]
             if r["split"] == split and r["set"] == "big_move"]
    con = duckdb.connect(str(screen_data.eval_db()), read_only=True)
    days = [r[0] for r in con.execute("select distinct trade_date from prices p join instruments i using (symbol) "
                                      "where i.currency = 'USD' order by 1").fetchall()]
    rows, text = [], {}
    from datetime import date
    for it in items:
        arts, (start, end) = NS.window_articles(con, days, date.fromisoformat(it["date"]))
        ranks = {v: [a["id"] for a in fn(arts, it["symbol"])] for v, fn in NS.VERSIONS.items()}
        pool = list(dict.fromkeys(i for v in NS.VERSIONS for i in ranks[v]))
        by_id = {a["id"]: a for a in arts}
        text[it["id"]] = [{k: by_id[i][k] for k in ("id", "time", "publisher", "symbols", "title", "summary")} for i in pool]
        rows.append({"id": it["id"], "date": it["date"], "symbol": it["symbol"], "asset_group": it["asset_group"],
                     "window": [start.isoformat(), end.isoformat()], "window_articles": len(arts),
                     "tagged": sum(it["symbol"] in a["symbols"] for a in arts), "ranks": ranks, "pool": pool})
    con.close()
    out = {"note": "E02 관련 표시 풀(기준선과 판마다 상위 5건의 합). 기사 번호만 둠(제목과 요약은 git 제외 파일).",
           "versions": list(NS.VERSIONS), "k": NS.K, "split": split, "items": rows}
    (HERE / f"pool_{split}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    GEN.mkdir(parents=True, exist_ok=True)
    (GEN / f"pool_{split}_text.json").write_text(json.dumps(text, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    n = sum(len(r["pool"]) for r in rows)
    both = sum(len(set(r["ranks"]["baseline"]) & set(r["ranks"]["h1"])) for r in rows)
    print(f"{split} 큰 움직임 날 {len(rows)}개, 풀 {n}건(기준선과 H1이 함께 고른 것 {both}건)")
    for r in rows:
        print(f"  {r['id']} {r['date']} {r['symbol']:5} 창 {r['window_articles']:4}건, 표시 {r['tagged']:3}건, "
              f"기준선 {len(r['ranks']['baseline'])}, H1 {len(r['ranks']['h1'])}, 풀 {len(r['pool'])}")


if __name__ == "__main__":
    main()
