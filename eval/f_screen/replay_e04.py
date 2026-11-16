#!/usr/bin/env python3
"""도구 층을 나눈 뒤 E04 기록의 도구 호출을 새 층으로 다시 재생해 결과가 같은지 봅니다(docs/ai/F-screen.md 2절 「구조」).

사용: .venv/bin/python eval/f_screen/replay_e04.py              모든 E04 실행 기록(eval/e04_agent/runs/*.jsonl)
      .venv/bin/python eval/f_screen/replay_e04.py --selftest   기록 값 하나를 바꾸면 잡는지

- 모델은 부르지 않습니다. 기록에 남은 (도구, 인자)를 가짜 세계 판(ai.tools.FAKE)으로 다시 부르고, 결과 전체
  (숫자, 경고, 날짜, 기사 번호)를 기록과 그대로 비교합니다.
- 의도한 변경은 아래 두 가지뿐이고, 그 밖의 차이는 모두 실패입니다. 기사 목록(번호, 시각, 순서)이 달라져도
  실패입니다. 바꿀 것 2(E02 검색 창)는 가짜 세계의 기사 고르기를 바꾸지 않아야 한다는 뜻입니다(기사 시각이
  창 경계 밖이라 E04의 날짜 맞춤과 결과가 같음).
  1. period_stats의 이름 바꿈(바꿀 것 5): 기록 쪽 이름을 새 이름으로 옮긴 뒤 비교합니다. 값은 같아야 합니다.
  2. 손익 도구의 FX_CARRIED: 도구가 화면과 같은 손익 계산(ai/pnl.py compute)을 써서, 환율을 넣지 않은 미국 종목
     매매가 환율 없는 날(한국 휴일)이면 이 경고가 더해집니다. 더해진 경고를 뺀 나머지(숫자, 다른 경고)는 같아야 하고,
     더해진 경고마다 그날 정말 환율이 없는지 가짜 세계에서 확인합니다.
- 자체 시험은 본 판정과 같은 함수(verdict)로 판정합니다.
"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai import tools as T  # noqa: E402

RUNS = ROOT / "eval" / "e04_agent" / "runs"
RENAME = {"period_stats": {"max_drawdown_pct": "price_max_drawdown_pct", "biggest_move_date": "largest_day_move_date",
                           "biggest_move_pct": "largest_day_move_pct"}}


def recorded():
    """기록 파일마다 (도구, 인자, 결과). 같은 (도구, 인자)가 여러 번 나오면 모두 둡니다(기록끼리도 같아야 함)."""
    out = []
    for path in sorted(RUNS.glob("*.jsonl")):
        if path.name == "ledger.jsonl":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            for c in (row.get("trace") or {}).get("tool_calls") or []:
                out.append({"file": path.name, "id": row.get("id"), "name": c["name"], "args": c["args"],
                            "result": c["result"]})
    return out


def normalize(name, result):
    r = copy.deepcopy(result)
    for old, new in RENAME.get(name, {}).items():
        if isinstance(r, dict) and old in r:
            r[new] = r.pop(old)
    return r


def diff(a, b, path=""):
    """다른 곳의 경로 목록(최대 몇 개)."""
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        return [f"{path or '/'}: 형 다름"]
    if isinstance(a, dict):
        out = [f"{path}/{k}: 기록에만 있음" for k in a.keys() - b.keys()]
        out += [f"{path}/{k}: 새 결과에만 있음" for k in b.keys() - a.keys()]
        for k in a.keys() & b.keys():
            out += diff(a[k], b[k], f"{path}/{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: 길이 {len(a)} → {len(b)}"]
        return [x for i, (u, v) in enumerate(zip(a, b)) for x in diff(u, v, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: 값 다름"]


def strip_fx_carried(name, result, toolset):
    """손익 도구 결과에서 FX_CARRIED 경고를 떼어 냅니다. 뗀 경고가 정말 환율 없는 날인지도 돌려줍니다."""
    if name not in ("whatif_pnl", "portfolio_pnl") or not isinstance(result, dict):
        return result, [], True
    r = copy.deepcopy(result)
    holders = [r] if name == "whatif_pnl" else r.get("positions", [])
    taken, legit = [], True
    data = toolset.data
    for h in holders:
        keep = []
        for w in h.get("warnings") or []:
            if w["code"] == "FX_CARRIED":
                taken.append(w)
                d = max(x for x in data.calendar if x <= w["trade_date"])
                legit &= d not in data.usdkrw and w["fx_date_used"] == max(x for x in data.fx_days if x <= d)
            else:
                keep.append(w)
        if "warnings" in h:
            h["warnings"] = keep
    return r, taken, legit


def compare(calls, toolset):
    same, intended, bad = [], [], []
    for c in calls:
        want = normalize(c["name"], c["result"])
        got = toolset.call(c["name"], c["args"])
        d = diff(want, got)
        if not d:
            same.append(c)
            continue
        stripped, taken, legit = strip_fx_carried(c["name"], got, toolset)
        if taken and not diff(want, stripped):
            intended.append({**c, "fx_carried": taken, "legit": legit})
            continue
        bad.append({**c, "diff": d[:6]})
    return same, intended, bad


def verdict(same, intended, bad):
    """통과: 다른 것이 없고, 더해진 FX_CARRIED가 모두 환율 없는 날."""
    return not bad and all(c["legit"] for c in intended)


def main():
    calls = recorded()
    uniq = {(c["name"], json.dumps(c["args"], sort_keys=True, ensure_ascii=False)) for c in calls}
    same, intended, bad = compare(calls, T.FAKE)
    by_tool = {}
    for group, rows in (("같음", same), ("의도한 변경", intended), ("다름", bad)):
        for c in rows:
            by_tool.setdefault(c["name"], {"같음": 0, "의도한 변경": 0, "다름": 0})[group] += 1
    print(f"기록 {len(calls)}번(서로 다른 호출 {len(uniq)}개), 기록 파일 {len({c['file'] for c in calls})}개, 모델 호출 0번\n")
    print("| 도구 | 같음 | 의도한 변경 | 다름 |")
    print("|---|---:|---:|---:|")
    for name, v in sorted(by_tool.items()):
        print(f"| {name} | {v['같음']} | {v['의도한 변경']} | {v['다름']} |")
    fx_rows = {}
    for c in intended:
        for w in c["fx_carried"]:
            fx_rows.setdefault((w["trade_date"], w["fx_date_used"], c["legit"]), set()).add(c["id"])
    for (d, used, legit), ids in sorted(fx_rows.items()):
        print(f"\n의도한 변경(FX_CARRIED): 매매일 {d}, 쓴 환율 날 {used}, 그날 환율 없음 {'맞음' if legit else '틀림'}, "
              f"질문 {', '.join(sorted(ids))}")
    for c in bad:
        print(f"\n다름: {c['file']} {c['id']} {c['name']} {c['args']}\n  " + "\n  ".join(c["diff"]))
    ok = verdict(same, intended, bad)
    print(f"\n재생 회귀: {'통과' if ok else '실패'}(같음 {len(same)}, 의도한 변경 {len(intended)}, 다름 {len(bad)})")
    return 0 if ok else 1


def selftest():
    """기록에서 숫자 하나, 경고 하나, 기사 번호 하나를 바꾸거나 기사를 모두 지우면 본 판정(verdict)이 실패하는지."""
    calls = recorded()
    found = {}
    for c in calls:
        r = c["result"]
        if "num" not in found and c["name"] == "price" and isinstance(r.get("close"), (int, float)):
            found["num"] = c
        if "warn" not in found and c["name"] == "whatif_pnl" and r.get("warnings"):
            found["warn"] = c
        if "cite" not in found and c["name"] == "search_events" and r.get("news"):
            found["cite"] = c
    if "cite" in found:
        found["cite_all"] = found["cite"]
    cases = []
    for key, c in found.items():
        m = copy.deepcopy(c)
        if key == "num":
            m["result"]["close"] = m["result"]["close"] + 0.01
        elif key == "warn":
            m["result"]["warnings"][0]["code"] = "OVERSOLD" if m["result"]["warnings"][0]["code"] != "OVERSOLD" else "BAD_QTY"
        elif key == "cite":
            m["result"]["news"][0]["id"] = "N99"
        else:
            m["result"]["news"] = []
        caught = not verdict(*compare([m], T.FAKE))
        cases.append((key, caught))
    ok = len(cases) == 4 and all(x for _, x in cases)
    names = {"num": "종가 바꾸기", "warn": "경고 코드 바꾸기", "cite": "기사 번호 바꾸기", "cite_all": "기사 모두 지우기"}
    for k, x in cases:
        print(f"{'통과' if x else '실패'}  {names[k]}: {'잡음' if x else '못 잡음'}")
    print("자체 시험:", "통과" if ok else "실패")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
