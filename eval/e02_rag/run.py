#!/usr/bin/env python3
"""E02 실행기: 평가 날짜마다 검색 판(기준선, H1)으로 답을 쓰고(ai/e02_answer.py) 규칙 지표를 셉니다(3절 「지표와 통과선」).

사용(저장소 루트에서):
  .venv/bin/python eval/e02_rag/run.py [--versions baseline,h1] [--ids E02-16]
  .venv/bin/python eval/e02_rag/run.py --split confirm --final --versions h1   확인용 16개를 마지막 판으로 한 번만

규칙으로 세는 것(이 스크립트):
- 숫자 일치: 답의 날짜, 등락률, 표준편차 배수, 큰 움직임 판정이 원본(data/raw/의 수정한 가격)으로 따로 센 값과 같음.
  큰 움직임 식은 pick_dates.py와 같고, 화면 파일과 ai/ 코드를 쓰지 않습니다.
- 인용 유효: 인용한 기사가 그 판의 검색 결과에 있고, 게시 시각이 검색 창(search_window.py) 안.
- 인과 표현(규칙): 모델이 쓴 부분의 채점기 규칙 위반 문장 수(판정 모델 쪽은 judge_answers.py).
- 모름 응답: 근거를 뺀 날에 「찾지 못했습니다」.
- 평범한 날: 「크게 움직인 날은 아닙니다」라고 밝힘(지어낸 사건이 없는지는 근거 충실도에서 봄).
- recall@5(잠정): 관련 표시(labels_{split}.json)의 판정 모델 표시로 셈. 사람 표본과 카파 0.7을 넘기 전에는 잠정입니다.
- 지연(빈 호출 기동 시간 x 호출 수를 뺌), 질문당 비용.

기록(답 글, 기사 제목)은 data/generated/e02/runs/(git 제외)입니다.
"""
import hashlib
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "eval" / "e04_agent"))

import duckdb  # noqa: E402
import pick_dates  # noqa: E402  평가 날짜를 고른 식 그대로(원본 읽기)
import search_window as SW  # noqa: E402
from score import violations  # noqa: E402

from ai import e02_answer as A  # noqa: E402
from ai import llm, screen_data  # noqa: E402

RUNS = ROOT / "data" / "generated" / "e02" / "runs"
WORKERS = 3
VARIANT = "e02w3"  # 답 규칙 e02_write_v3(v2: e02w2)


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def truth(sym, d):
    """원본에서 따로 센 (등락률 %, 표준편차 배수, 큰 움직임). pick_dates.load_symbol과 같은 식."""
    prices = json.loads((pick_dates.PRICES / f"us_{sym}_{pick_dates.TAG}_adjtrue.json").read_text(encoding="utf-8"))["results"]
    days = pick_dates.trading_days(prices)
    i = days.index(date.fromisoformat(d))
    rets = [None] + [prices[k]["c"] / prices[k - 1]["c"] - 1 for k in range(1, len(prices))]
    sd = statistics.stdev(rets[i - pick_dates.LOOKBACK:i])
    return rets[i] * 100, abs(rets[i]) / sd, abs(rets[i]) > 2 * sd, days


def score(item, ans, trace, days):
    pct, ratio, big, _ = truth(item["symbol"], item["date"])
    mv = ans["move"]
    numbers_ok = (mv["judged"] and abs(mv["pct"] - pct) <= 0.005 + 1e-9 and abs(mv["ratio"] - ratio) <= 0.05 + 1e-9
                  and mv["big"] == big and f"{abs(mv['pct']):.2f}%" in ans["text"] and item["date"] in ans["text"])
    start, end = SW.window(days, date.fromisoformat(item["date"]))
    cites_ok = all(c["id"] in trace["retrieved"] and
                   start <= datetime.fromisoformat(c["time"].replace("Z", "+00:00")) < end for c in ans["citations"])
    model_part = ans["text"].split("(2배 초과가 큰 움직임).", 1)[-1]
    cause = [s for k, s in violations(A.CITE.sub("", model_part)) if k == "cause"]
    row = {"id": item["id"], "set": item["set"], "symbol": item["symbol"], "version": trace["version"],
           "status": ans["status"], "decided_by": trace["decided_by"], "rewrite": bool(trace["rewrite"]),
           "numbers_ok": numbers_ok, "big_expected": item["set"] != "calm", "big_said": mv["big"],
           "citations": len(ans["citations"]), "cites_ok": cites_ok, "cause_rule": len(cause),
           "seconds": round(trace["seconds"], 2), "n_llm": len(trace["llm"]),
           "cost_usd": sum(c["cost_usd"] or 0 for c in trace["llm"])}
    if item["set"] == "no_evidence":
        row["unknown_ok"] = ans["status"] == "unknown" and A.TEXT["not_found"] in ans["text"]
    if item["set"] == "calm":
        row["calm_ok"] = mv["big"] is False and "크게 움직인 날은 아닙니다" in ans["text"]
    return row


def recall(rows, split, pool):
    """판마다 큰 움직임 날의 recall@5(판정 모델 표시, 잠정). 관련 기사가 0건인 날은 따로 셈."""
    path = HERE / f"labels_{split}.json"
    if not path.exists():
        return None
    judge = json.loads(path.read_text(encoding="utf-8"))["judge"]
    out = {}
    for v in {r["version"] for r in rows}:
        vals, empty = [], []
        for it in pool["items"]:
            rel = {a for a, ok in judge.get(it["id"], {}).items() if ok}
            if not rel:
                empty.append(it["id"])
                continue
            vals.append(len(rel & set(it["ranks"][v])) / len(rel))
        out[v] = {"recall_at_5": round(sum(vals) / len(vals), 3) if vals else None, "days": len(vals), "no_relevant": empty}
    return out


def main():
    split = arg("--split", "dev")
    versions = arg("--versions", "baseline,h1").split(",")
    items = [r for r in json.loads((HERE / "eval_dates.json").read_text(encoding="utf-8"))["items"] if r["split"] == split]
    if arg("--ids"):
        want = arg("--ids").split(",")
        items = [r for r in items if r["id"] in want]
        if len(items) != len(want):
            sys.exit(f"--ids의 날짜는 모두 --split {split}이어야 합니다")
    RUNS.mkdir(parents=True, exist_ok=True)
    if split == "confirm":
        if "--final" not in sys.argv or arg("--ids") or len(versions) != 1:
            sys.exit("확인용은 마지막 판 하나로 --final, 전체를 한 번만 돌립니다")
        if list(RUNS.glob(f"{VARIANT}_confirm_*.jsonl")):
            sys.exit("확인용 기록이 이미 있습니다. 다시 돌리지 않습니다")
    con = duckdb.connect(str(screen_data.eval_db()), read_only=True)
    days = [r[0] for r in con.execute("select distinct trade_date from prices p join instruments i using (symbol) "
                                      "where i.currency = 'USD' order by 1").fetchall()]
    rows_by = {}
    for s in {r["symbol"] for r in items}:
        rows_by[s] = {d.isoformat(): {"adj_close": a, "close": c} for d, a, c in con.execute(
            "select trade_date, adj_close, close from prices where symbol = ? order by 1", [s]).fetchall()}
    lock_days = truth(items[0]["symbol"], items[0]["date"])[3]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{VARIANT}_{split}{'_part' if arg('--ids') else ''}_{stamp}"
    boot, _ = llm.boot_seconds(kind="real")
    jobs = [(it, v) for v in versions for it in items]

    def one(job):
        it, v = job
        cur = con.cursor()  # DuckDB 연결 하나를 여러 스레드가 함께 쓰면 「busy」로 거절하므로 스레드마다 커서
        try:
            ans, tr = A.answer(cur, days, rows_by[it["symbol"]], it["symbol"], it["date"], v,
                               drop_evidence=it["set"] == "no_evidence", tag=f"{it['id']}:{v}")
        finally:
            cur.close()
        return {"item": it, "answer": ans, "trace": tr, "score": score(it, ans, tr, lock_days)}

    with ThreadPoolExecutor(WORKERS) as ex:
        out = list(ex.map(one, jobs))
    con.close()
    with (RUNS / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False, default=str) + "\n")
    rows = [o["score"] for o in out]
    pool_path = HERE / f"pool_{split}.json"
    rec = recall(rows, split, json.loads(pool_path.read_text(encoding="utf-8"))) if pool_path.exists() else None
    cost, calls = llm.spent()
    summary = {"variant": VARIANT, "split": split, "versions": versions, "boot_s": boot, "recall_judge_provisional": rec,
               "code_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in
                               [ROOT / "ai/e02_answer.py", ROOT / "ai/news_search.py", ROOT / "ai/prompts/e02_write_v3.txt",
                                Path(__file__)]},
               "ledger_total_usd": round(cost, 4), "ledger_calls": calls, "by_version": {}}
    for v in versions:
        rs = [r for r in rows if r["version"] == v]
        lat = sorted(max(0.0, r["seconds"] - boot * r["n_llm"]) for r in rs if r["n_llm"])
        pick = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else 0.0
        big = [r for r in rs if r["set"] == "big_move"]
        m = {"n": len(rs),
             "numbers_ok": f"{sum(r['numbers_ok'] for r in rs)}/{len(rs)}",
             "cites_ok": f"{sum(r['cites_ok'] for r in rs)}/{len(rs)}",
             "cause_rule": sum(r["cause_rule"] for r in rs),
             "unknown_ok": f"{sum(r.get('unknown_ok', False) for r in rs if r['set'] == 'no_evidence')}/{sum(r['set'] == 'no_evidence' for r in rs)}",
             "calm_ok": f"{sum(r.get('calm_ok', False) for r in rs if r['set'] == 'calm')}/{sum(r['set'] == 'calm' for r in rs)}",
             "answered_big": f"{sum(r['status'] == 'answered' for r in big)}/{len(big)}",
             "rewrite": sum(r["rewrite"] for r in rs), "not_verified": sum(r["decided_by"] == "code:not_verified" for r in rs),
             "p50_s": round(statistics.median(lat), 1) if lat else None, "p95_s": round(pick(0.95), 1) if lat else None,
             "cost_per_q": round(sum(r["cost_usd"] for r in rs) / len(rs), 4)}
        summary["by_version"][v] = m
    (RUNS / f"{name}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"| 판 | " + " | ".join(versions) + " |\n|---|" + "---|" * len(versions))
    for k, label in [("numbers_ok", "숫자 일치"), ("cites_ok", "인용 유효"), ("cause_rule", "인과 표현(규칙)"),
                     ("unknown_ok", "모름 응답"), ("calm_ok", "평범한 날 밝힘"), ("answered_big", "큰 움직임 날 답함"),
                     ("rewrite", "다시 쓰기"), ("not_verified", "검사 실패로 숨김"), ("p50_s", "p50(초)"),
                     ("p95_s", "p95(초)"), ("cost_per_q", "질문당 비용($)")]:
        print(f"| {label} | " + " | ".join(str(summary['by_version'][v][k]) for v in versions) + " |")
    if rec:
        print("| recall@5(판정 모델, 잠정) | " + " | ".join(f"{rec[v]['recall_at_5']}({rec[v]['days']}일)" for v in versions)
              + " |")
        print(f"관련 기사가 하나도 없는 큰 움직임 날: {rec[versions[0]]['no_relevant']}")
    print(f"기동 {boot:.1f}초. 기록 {(RUNS / name).relative_to(ROOT)}.jsonl, 장부 누적 ${cost:.2f}, {calls}번")


if __name__ == "__main__":
    main()
