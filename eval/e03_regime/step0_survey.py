#!/usr/bin/env python3
"""E03 0단계 조사: 받을 수 있는 기간만 잰다. 사용: python3 step0_survey.py

수익률과 가격을 출력하거나 저장하지 않는다. 평가 기준을 확정하기 전에 판정 데이터를 보지 않기 위해서다.
ECOS 키가 없으면 공개 예시 키(앞 10행까지)로 조회한다. 결과는 step0_result.json.
ETF 시세를 E02 조사로 받아 두었으면, 겹치지 않는 비슷한 때가 5번을 채우는 날의 비율도 낸다.
"""
import json
import math
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from power_sim import K_NAIVE, WARMUP, neighbors, prepare  # noqa: E402
from step0_util import ROOT, get_json, load_key  # noqa: E402

HERE = Path(__file__).parent
ECOS_TABLES = {"802Y001": ["KOSPI지수"], "731Y001": ["원/미국달러(매매기준율)"],
               "817Y002": ["콜금리(1일, 전체거래)", "국고채(3년)", "국고채(10년)"]}
DOMESTIC = {"005930": "삼성전자", "000660": "SK하이닉스"}
FSC = "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
ETFS = ["SPY", "QQQ", "IWM", "SHY", "TLT", "HYG", "GLD", "USO", "UUP", "EFA", "EEM", "IBIT"]


def ecos():
    key = load_key("ECOS_API_KEY")
    size = 100 if key else 10
    rows = []
    for table, names in ECOS_TABLES.items():
        code, data = get_json(f"https://ecos.bok.or.kr/api/StatisticItemList/{key or 'sample'}/json/kr/1/{size}/{table}")
        items = ((data or {}).get("StatisticItemList") or {}).get("row", [])
        if not items:
            raise SystemExit(f"ECOS {table} 조회 실패: HTTP {code}, {(data or {}).get('RESULT', {}).get('MESSAGE', '')}")
        for it in items:
            if it["ITEM_NAME"] in names and it["CYCLE"] == "D":
                rows.append({"table": table, "item": it["ITEM_CODE"], "name": it["ITEM_NAME"],
                             "start": it["START_TIME"], "end": it["END_TIME"], "rows": it["DATA_CNT"]})
    return {"used_key": bool(key), "rows": rows}


def fsc_call(key, code, page):
    url = f"{FSC}?serviceKey={key}&resultType=json&numOfRows=1&pageNo={page}&likeSrtnCd={code}"
    status, data = get_json(url)
    body = ((data or {}).get("response") or {}).get("body")
    if body is None:
        raise SystemExit(f"공공데이터포털 조회 실패: HTTP {status}")
    items = (body.get("items") or {}).get("item") or []
    return body.get("totalCount", 0), (items[0].get("basDt") if items else None)


def domestic():
    key = load_key("DATA_GO_KR_SERVICE_KEY")
    if not key:
        return {"skipped": "키 없음"}
    if "%" not in key:
        key = quote(key, safe="")
    out = {}
    for code, name in DOMESTIC.items():
        total, _ = fsc_call(key, code, 1)
        ends = [fsc_call(key, code, p)[1] for p in (1, total)]
        out[code] = {"name": name, "rows": total, "first": min(ends), "last": max(ends)}
    return out


def etf_episodes():
    raw = ROOT / "data" / "raw" / "e02_step0"
    out = {}
    for h in (5, 20):
        counts = []
        days = None
        for sym in ETFS:
            f = raw / f"{sym}.prices.json"
            if not f.exists():
                return {"skipped": "E02 조사로 시세를 받은 뒤 다시 돌립니다"}
            closes = [r["c"] for r in json.loads(f.read_text(encoding="utf-8"))]
            r = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            days = max(days or 0, len(r))
            sig, ret, _, _ = prepare(r, h)
            for t in range(WARMUP, len(r) - h, h):
                last, c = None, 0
                for i in sorted(neighbors(sig, ret, t, h, K_NAIVE)):
                    if last is None or i - last >= h:
                        c += 1
                        last = i
                counts.append(c)
        out[str(h)] = {"longest_series_days": days, "tests": len(counts),
                       "enough_pct": round(sum(c >= 5 for c in counts) / len(counts) * 100, 1) if counts else None,
                       "mean_episodes": round(sum(counts) / len(counts), 1) if counts else None}
    return out


def main():
    result = {"note": "기간과 행 수만 둡니다. 가격과 수익률은 보지 않았습니다.",
              "ecos": ecos(), "domestic": domestic(), "etf_episodes": etf_episodes()}
    (HERE / "step0_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
