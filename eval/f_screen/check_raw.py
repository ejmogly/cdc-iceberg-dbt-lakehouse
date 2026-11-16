#!/usr/bin/env python3
"""F 화면 1단계: 받은 원본(data/raw/)의 행 수, 기간, 분할을 확인합니다(docs/ai/F-screen.md 3절).

사용: .venv/bin/python eval/f_screen/check_raw.py --start 2024-11-13 --end 2026-11-12
      .venv/bin/python eval/f_screen/check_raw.py --selftest   가짜 원본에 분할을 심어 잡는지 확인

- 미국 종목은 수정 안 한 가격(adjfalse)과 수정한 가격(adjtrue)의 종가 비율을 날마다 셉니다.
  비율이 앞날보다 1% 넘게 바뀐 날을 분할 후보로 냅니다. Massive의 수정은 분할만 반영합니다(분배금은 넣지 않음).
- 가격 값은 출력하지 않습니다(재배포 금지). 날짜, 행 수, 비율만 냅니다.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
INSTRUMENTS = ROOT / "loaders" / "instruments.csv"


def symbols():
    rows = INSTRUMENTS.read_text(encoding="utf-8").splitlines()[1:]
    return [(r.split(",")[0], r.split(",")[2]) for r in rows if r]


def us_closes(sym, tag, adjusted):
    path = RAW / "daily_prices" / f"us_{sym}_{tag}_adj{adjusted}.json"
    if not path.exists():
        return None
    out = {}
    for r in json.loads(path.read_text(encoding="utf-8")).get("results") or []:
        d = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date()
        out[d] = r["c"]
    return out


def kr_rows(sym, tag):
    path = RAW / "daily_prices" / f"kr_{sym}_{tag}.json"
    if not path.exists():
        return None
    return sorted({r["basDt"] for r in json.loads(path.read_text(encoding="utf-8"))})


def selftest():
    """가짜 원본 두 종목: 다섯째 날부터 10:1 분할을 심은 것(잡아야 함)과 분할 없는 것(잡으면 안 됨)."""
    import contextlib
    import io
    import tempfile
    global RAW
    saved = RAW
    with tempfile.TemporaryDirectory() as tmp:
        RAW = Path(tmp)
        (RAW / "daily_prices").mkdir()
        day, t0 = 86400000, 1717372800000  # 2024-06-03 00:00 UTC
        for sym, split in (("NVDA", True), ("SPY", False)):
            raw = [{"t": t0 + i * day, "c": (1000.0 + i) / (10 if split and i >= 5 else 1)} for i in range(10)]
            adj = [{"t": t0 + i * day, "c": (1000.0 + i) / (10 if split else 1)} for i in range(10)]
            for kind, rows in (("false", raw), ("true", adj)):
                (RAW / "daily_prices" / f"us_{sym}_20240603_20240612_adj{kind}.json").write_text(json.dumps({"results": rows}))
        sys.argv = ["x", "--start", "2024-06-03", "--end", "2024-06-12"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main()
        RAW = saved
    out = buf.getvalue()
    ok = "분할 후보 종목: NVDA" in out and "2024-06-08 비율 10 → 1" in out
    print("자체 시험:", "통과(심은 10:1 분할을 그날 잡고, 분할 없는 종목은 잡지 않음)" if ok else "실패\n" + out)
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    a = p.parse_args()
    tag = a.start.replace("-", "") + "_" + a.end.replace("-", "")
    missing, splits = [], []
    print("| 종목 | 시장 | 행 수 | 처음 | 끝 | 분할 후보 |")
    print("|---|---|---:|---|---|---|")
    for sym, market in symbols():
        if market == "KRX":
            days = kr_rows(sym, tag)
            if days is None:
                missing.append(f"kr_{sym}")
                continue
            print(f"| {sym} | 국내 | {len(days)} | {days[0]} | {days[-1]} | 해당 없음 |")
            continue
        raw, adj = us_closes(sym, tag, "false"), us_closes(sym, tag, "true")
        if raw is None or adj is None:
            missing.append(f"us_{sym}")
            continue
        both = sorted(set(raw) & set(adj))
        found, prev = [], None
        for d in both:
            ratio = raw[d] / adj[d] if adj[d] else None
            if ratio and prev and abs(ratio / prev - 1) > 0.01:
                found.append(f"{d} 비율 {prev:.4g} → {ratio:.4g}")
            prev = ratio or prev
        if found:
            splits.append(sym)
        only = len(set(raw) ^ set(adj))
        note = "; ".join(found) if found else "없음"
        if only:
            note += f" (두 판의 날짜가 {only}일 다름)"
        print(f"| {sym} | 미국 | {len(raw)} | {both[0]} | {both[-1]} | {note} |")
    print()
    print(f"없는 원본: {', '.join(missing) if missing else '없음'}")
    print(f"분할 후보 종목: {', '.join(splits) if splits else '없음'}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
