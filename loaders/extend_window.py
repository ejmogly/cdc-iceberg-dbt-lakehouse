#!/usr/bin/env python3
"""화면 데이터 기간을 늘립니다: 새 원본 구간을 받아 원천 PostgreSQL에 넣고 loaders/window.json에 구간을 더합니다.

사용: .venv/bin/python loaders/extend_window.py                 끝날은 어제(서울)
      .venv/bin/python loaders/extend_window.py --end 2026-11-25
      .venv/bin/python loaders/extend_window.py --plan          할 일만 보이고 아무것도 받지 않음

순서(2026-11-19에 손으로 한 번 한 것, 결정 기록 0015):
1. 새 구간은 지금 마지막 구간의 끝날부터 하루 겹칩니다. 받을 때 아직 나오지 않은 종가(미국 장 마감 뒤에 나옴)를 다음에 채우려는 것이고,
   겹친 날은 원본끼리 값이 같아야 합니다(내보내기 검사 「원본 구간이 겹치는 날」).
2. 적재기를 --dry-run으로 돌려 data/raw/에 원본만 받습니다: 국내 시세, 미국 시세 두 판(수정 안 한 판, 수정한 판, 종목마다 기간 조회),
   환율, 전체 기사. 미국은 무료 요금제(분당 5번)라 몇 분 걸립니다.
3. 받은 파일이 모두 있는지 보고, 같은 원본으로 --from-raw 적재(시세는 수정 안 한 판, 환율)를 합니다. VM과 원천 DB가 떠 있어야 합니다.
4. 구간을 loaders/window.json에 더합니다(Airflow 컨테이너에도 붙는 폴더). 그 뒤 make lake-refresh(Bronze 작업이 떠 있어야 함)가
   Silver, Gold를 거쳐 화면 파일을 새로 내보내고 검사합니다. 평가는 얼린 파일(screen_eval.duckdb)이라 바뀌지 않습니다.

- 국내 시세는 거래일 다음 영업일 오후 1시 뒤에 나옵니다. 끝날의 국내 행이 없으면 멈추고 구간을 더하지 않습니다.
- 가격 값은 출력하지 않습니다. 행 수와 날짜만 냅니다.
"""
import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
WINDOW = ROOT / "loaders" / "window.json"
RAW = ROOT / "data" / "raw"
PY = sys.executable


def symbols():
    import csv
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r["symbol"] for r in rows if r["market"] == "KRX"], [r["symbol"] for r in rows if r["market"] == "US"]


def files(tag):
    kr, us = symbols()
    out = [RAW / "daily_prices" / f"kr_{s}_{tag}.json" for s in kr]
    out += [RAW / "daily_prices" / f"us_{s}_{tag}_adj{k}.json" for s in us for k in ("false", "true")]
    return out + [RAW / "fx_rates" / f"USDKRW_{tag}.json", RAW / "news_massive" / f"ALL_{tag}.json"]


def steps(start, end):
    a = ["--start", start, "--end", end]
    fetch = [["loaders/daily_prices.py", "--market", "kr", *a, "--dry-run"],
             ["loaders/fx_rates.py", *a, "--dry-run"],
             ["loaders/daily_prices.py", "--market", "us", "--mode", "ticker", "--adjusted", "false", *a, "--dry-run"],
             ["loaders/daily_prices.py", "--market", "us", "--mode", "ticker", "--adjusted", "true", *a, "--dry-run"],
             ["loaders/news_massive.py", "--all-articles", *a, "--dry-run"]]
    load = [["loaders/daily_prices.py", "--market", "kr", *a, "--from-raw"],
            ["loaders/daily_prices.py", "--market", "us", "--mode", "ticker", "--adjusted", "false", *a, "--from-raw"],
            ["loaders/fx_rates.py", *a, "--from-raw"]]
    return fetch, load


def last_day(tag, sym):
    rows = json.loads((RAW / "daily_prices" / f"kr_{sym}_{tag}.json").read_text(encoding="utf-8"))
    return max((datetime.strptime(r["basDt"], "%Y%m%d").date() for r in rows), default=None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--end", type=date.fromisoformat, help="새 끝날(그날 포함). 기본은 어제(서울)")
    ap.add_argument("--plan", action="store_true", help="할 일만 보이고 받지 않음")
    args = ap.parse_args()
    w = json.loads(WINDOW.read_text(encoding="utf-8"))
    prev_end = date.fromisoformat(w["parts"][-1]["end"])
    end = args.end or (datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1))
    if end <= prev_end:
        sys.exit(f"이미 {prev_end}까지 있습니다. 늘릴 끝날({end})이 그보다 늦어야 합니다")
    start = prev_end  # 하루 겹침
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"
    fetch, load = steps(start.isoformat(), end.isoformat())
    print(f"새 구간 {start} ~ {end}(앞 구간의 끝날부터 하루 겹침), 원본 파일 {len(files(tag))}개")
    if args.plan:
        for s in fetch + load:
            print("  " + " ".join(["python", *s]))
        return 0
    for s in fetch:
        print("==", " ".join(s[:4]), flush=True)
        if subprocess.run([PY, *s], cwd=ROOT).returncode != 0:
            sys.exit("받기가 실패했습니다. 구간을 더하지 않았습니다")
    missing = [p.relative_to(ROOT) for p in files(tag) if not p.exists()]
    if missing:
        sys.exit(f"받은 파일이 모자랍니다({len(missing)}개, 예: {missing[0]}). 구간을 더하지 않았습니다")
    kr, _ = symbols()
    short = [s for s in kr if last_day(tag, s) != end and end.weekday() < 5]
    if short:  # 국내 시세는 다음 영업일 오후 1시 뒤에 나옴. 끝날이 국내 휴장일이면 이 검사가 멈추므로 --end를 앞당김
        sys.exit(f"국내 시세가 {end}까지 오지 않았습니다({', '.join(short)}). 나온 뒤 다시 하거나 --end를 앞당기세요")
    for s in load:
        print("==", " ".join(s[:4]), flush=True)
        if subprocess.run([PY, *s], cwd=ROOT).returncode != 0:
            sys.exit("원천 적재가 실패했습니다(VM과 원천 DB가 떠 있나요). 받은 원본은 남았고 구간은 더하지 않았습니다")
    w["parts"].append({"start": start.isoformat(), "end": end.isoformat(),
                       "got": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(), "note": "extend_window.py"})
    WINDOW.write_text(json.dumps(w, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"loaders/window.json에 {start} ~ {end}를 더했습니다. 다음: make lake-refresh(Bronze 작업이 떠 있어야 함)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
