"""F 화면 평가의 독립 정답이 쓰는 데이터 묶음을 `data/raw/` 원본에서 바로 만듭니다(docs/ai/F-screen.md 4절).

화면은 원천 PostgreSQL을 거쳐 내보낸 파일을 읽습니다. 이 모듈은 적재와 내보내기를 거치지 않고 받은 원본 응답을
직접 읽어, 적재 단계의 실수(날짜 변환, 환율 맞춤)가 정답으로 옮겨 가지 않게 합니다. 화면 코드는 이 모듈을 쓰지 않습니다.

모양은 E01 독립 계산기(eval/e01_pnl/make_expected.py)의 fixture와 같습니다.
- calendar: 19종목 시세 날짜와 환율 날짜의 합집합(손익 SQL `pnl_market_daily` 주석의 정의와 같음)
- symbols: 종목마다 통화와 날짜별 종가, 저가, 고가. 기준 종목 BENCH는 SPY와 같은 값
- usdkrw: 날짜별 원/달러 매매기준율
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
TAG = "20241113_20261112"


def _iso(yyyymmdd):
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _instruments():
    rows = (ROOT / "loaders" / "instruments.csv").read_text(encoding="utf-8").splitlines()[1:]
    return [(r.split(",")[0], r.split(",")[3]) for r in rows if r]


def _us(sym):
    path = RAW / "daily_prices" / f"us_{sym}_{TAG}_adjfalse.json"
    out = {}
    for r in json.loads(path.read_text(encoding="utf-8"))["results"]:
        d = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date().isoformat()
        out[d] = {"close": str(r["c"]), "low": str(r["l"]), "high": str(r["h"])}
    return out


def _kr(sym):
    path = RAW / "daily_prices" / f"kr_{sym}_{TAG}.json"
    out = {}
    for r in json.loads(path.read_text(encoding="utf-8")):
        out[_iso(r["basDt"])] = {"close": r["clpr"], "low": r["lopr"], "high": r["hipr"]}
    return out


def _fx():
    payload = json.loads((RAW / "fx_rates" / f"USDKRW_{TAG}.json").read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload["StatisticSearch"]["row"]
    return {_iso(r["TIME"]): r["DATA_VALUE"] for r in rows}


def load():
    symbols = {}
    for sym, currency in _instruments():
        rows = _kr(sym) if currency == "KRW" else _us(sym)
        symbols[sym] = {"currency": currency, "rows": rows}
    symbols["BENCH"] = {"currency": "USD", "rows": symbols["SPY"]["rows"]}
    usdkrw = _fx()
    days = set(usdkrw)
    for sym, v in symbols.items():
        days |= set(v["rows"])
    return {"calendar": sorted(days), "symbols": symbols, "usdkrw": usdkrw}
