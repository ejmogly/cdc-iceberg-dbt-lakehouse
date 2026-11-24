"""화면 데이터 파일을 만들고 검사합니다(docs/ai/F-screen.md 2절 「구조」, 화면 10번 칸 「파이프라인 상태」).

사용: .venv/bin/python -m ai.screen_data export        Gold 마트(Spark, VM 필요)에서 내보내고 원천과 원본에 대조
      .venv/bin/python -m ai.screen_data export --if-changed
                                                       같되, 내용이 같고 검사가 통과하면 지금 파일을 그대로 둠(Airflow)
      .venv/bin/python -m ai.screen_data golden        E01 골든의 가짜 시세로 같은 구조의 파일(골든 대조용)
      .venv/bin/python -m ai.screen_data status [파일]  저장한 검사 결과와 지문 대조
      .venv/bin/python -m ai.screen_data freeze        평가용 파일(screen_eval.duckdb)을 첫 구간 원본에서 다시 만들고
                                                       고정한 내용 지문과 대조(평가 시험은 이 파일을 엶, eval_db())
      .venv/bin/python -m ai.screen_data --selftest    일부러 깬 데이터를 잡는지(행 지우기, 환율 하루 빼기, 종가 바꾸기,
                                                       내보낸 뒤 파일 고치기). VM 없이 원본을 Gold와 원천 대신 씀

- 시세와 환율은 레이크하우스의 Gold 마트(dbt 모델 mart_symbol_daily, mart_fx_daily)에서 읽습니다(11/18 화면 개편 뒤 1안).
  원천 PostgreSQL → Debezium, Kafka → Flink Bronze → Spark MERGE Silver → dbt Gold를 거친 값입니다.
- 내보낼 때 두 번 대조합니다. Gold와 원천(레이크 경로가 값을 바꾸거나 빠뜨리지 않았는지), Gold와 받은 원본(data/raw/,
  적재 실수, 빠진 행, 바뀐 값). 하나라도 다르면 검사가 실패합니다. 원본 읽기는 독립 정답(eval/f_screen/real_world.py)과
  공유하지 않습니다.
- 수정한 가격(adj_close)과 기사는 원천 DB에 없어(설계서 6절 2, 6번) data/raw/에서 바로 넣습니다.
- 검사 결과는 파일의 checks 테이블에, 내용의 지문은 meta에 둡니다. 서버는 열 때 지문을 다시 셉니다.
  검사가 하나라도 실패하거나 지문이 다르면 화면은 숫자 대신 실패 항목을 보입니다.
- 가격 값은 출력하지 않습니다(재배포 금지). 행 수, 날짜, 개수만 냅니다.
- 데이터 기간은 받은 원본의 구간 목록(data/generated/window.json, git 밖)으로 늘립니다. 목록이 없으면 첫 구간(평가 기준 2년치)만
  씁니다. 첫 구간 원본은 data/raw/에, 늘린 구간 원본은 data/raw/window/에 있습니다(Mac이 받고 Airflow window_extend가 더함,
  docs/decisions/0016). 평가는 정답을 고정한 때의 얼린 파일(EVAL)을 쓰고, 내보낼 때 EVAL_END까지의 값이 그 파일과 같은지
  (늦게 나온 행을 더한 것만 허용) 대조합니다(docs/decisions/0015).
"""
import argparse
import csv
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GEN = ROOT / "data" / "generated"
OUT = GEN / "screen.duckdb"
GOLDEN_OUT = GEN / "screen_golden.duckdb"
# 늘린 구간의 원본(Mac의 loaders/extend_window.py fetch가 받음)과 구간 목록(Airflow window_extend의 add_part가 적음).
# Airflow 컨테이너에는 data/raw/가 읽기 전용, data/generated/가 쓰기로 붙습니다(docs/decisions/0016)
RAW_WINDOW = RAW / "window"
EVENTS_FILE = RAW / "schedule" / "events_20241113_20261231.json"
SKIP = 99  # Airflow BashOperator가 「건너뜀」으로 보는 종료 코드(skip_on_exit_code 기본값)
WINDOW_FILE = GEN / "window.json"
# 평가(잠근 정답과 견주는 시험)가 쓰는 얼린 화면 파일. 정답을 고정한 때의 데이터(2024-11-13 ~ 2026-11-12)와 내용이 같아야 합니다.
# 지문은 내용 테이블(시세, 환율, 달력, 기사, 일정, 종목)의 sha256이라 값 자체는 담지 않습니다(docs/decisions/0015).
EVAL = GEN / "screen_eval.duckdb"
EVAL_END = "2026-11-12"
EVAL_DIGEST = "2acfe7cf2008cb9829c56a8d5155aa0d43a2fe79ac48f0ec4dcc060fc484bf66"
BASE = ("2024-11-13", EVAL_END)   # 첫 구간(평가 기준 2년치, 원본 지문은 eval/f_screen/lock.json). 구간 목록이 없으면 이것만
BASE_TAG = BASE[0].replace("-", "") + "_" + BASE[1].replace("-", "")
ET = ZoneInfo("America/New_York")
Q4 = Decimal("0.0001")
REAL_BENCH = "SPY"

SCHEMA = """
create table meta (key varchar primary key, value varchar);
create table instruments (symbol varchar primary key, name varchar, market varchar, currency varchar,
                          asset_class varchar, grp varchar);
create table prices (symbol varchar, trade_date date, open decimal(18, 4), high decimal(18, 4), low decimal(18, 4),
                     close decimal(18, 4), adj_close decimal(18, 4), volume bigint, primary key (symbol, trade_date));
create table fx (rate_date date primary key, rate decimal(12, 4));
create table calendar (cal_date date primary key);
create table news (id varchar primary key, published_utc timestamp, title varchar, description varchar,
                   publisher varchar, tickers varchar[], url varchar);
create table events (id varchar primary key, event_date date, kind varchar, title varchar, source varchar,
                     local_time varchar, tz varchar);
create table checks (seq integer, name varchar, ok boolean, value varchar, detail varchar);
"""
CONTENT = [("instruments", "symbol"), ("prices", "symbol, trade_date"), ("fx", "rate_date"),
           ("calendar", "cal_date"), ("news", "id"), ("events", "id")]


def read_window(path=WINDOW_FILE):
    """받은 원본의 구간 목록(data/generated/window.json)을 [(시작, 끝)]으로 돌려줍니다. 목록 파일이 없으면 첫 구간만입니다.
    화면 데이터 기간은 첫 구간의 시작부터 마지막 구간의 끝까지입니다. 구간의 원본 파일 이름은 적재기의 --start, --end
    그대로입니다(kr_<종목>_<시작>_<끝>.json 등). 첫 구간은 BASE여야 하고, 뒤 구간은 앞 구간의 끝 다음 날보다 늦게 시작할 수
    없고(빈 날 없음), 끝은 앞 구간보다 늦어야 합니다. 겹치는 날은 원본끼리 값이 같아야 하고, 늘릴 때와 내보낼 때 검사합니다
    (늦게 나온 미국 종가를 받으려고 하루 겹쳐 받음)."""
    from datetime import date, timedelta
    if not path.exists():
        return [BASE]
    parts = [(p["start"], p["end"]) for p in json.loads(path.read_text(encoding="utf-8"))["parts"]]
    if not parts or parts[0] != BASE:
        raise ValueError(f"{path.name}의 첫 구간이 평가 기준 {BASE[0]} ~ {BASE[1]}가 아닙니다")
    for i, (s, e) in enumerate(parts):
        a, b = date.fromisoformat(s), date.fromisoformat(e)
        if a > b:
            raise ValueError(f"{path.name} {i + 1}번 구간의 시작이 끝보다 늦습니다")
        if i and (a > date.fromisoformat(parts[i - 1][1]) + timedelta(days=1) or b <= date.fromisoformat(parts[i - 1][1])):
            raise ValueError(f"{path.name} {i + 1}번 구간이 앞 구간에 이어지지 않습니다(빈 날이 있거나 끝이 늘지 않음)")
    return parts


PARTS = read_window()
START, END = PARTS[0][0], PARTS[-1][1]
TAGS = [s.replace("-", "") + "_" + e.replace("-", "") for s, e in PARTS]


def raw_dir(tag):
    """구간의 원본 폴더. 첫 구간은 data/raw/, 늘린 구간은 data/raw/window/입니다."""
    return RAW if tag == BASE_TAG else RAW_WINDOW


# ---------- 읽기 ----------

def instruments():
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8", newline="") as f:
        return [{"symbol": r["symbol"], "name": r["name"], "market": r["market"], "currency": r["currency"],
                 "asset_class": r["asset_class"], "grp": r["group"]} for r in csv.DictReader(f)]


def _q(v):
    return Decimal(str(v)).quantize(Q4)


def _et_date(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET).date()


def read_raw(insts, tags=None, overlap=None):
    """받은 원본에서 시세(시가, 고가, 저가, 종가, 거래량), 수정 종가, 환율을 읽습니다. 키는 (종목, date).
    구간(tags, 기본은 구간 목록의 모든 구간)을 차례로 읽어 합칩니다. overlap(dict)을 주면 두 구간에 함께 있는
    값의 수(same)와 값이 다른 키(diff)를 담습니다. 다른 값은 앞 구간의 값을 남깁니다(내보내기 검사가 실패함)."""
    prices, adj, fx = {}, {}, {}
    stat = overlap if overlap is not None else {}
    stat.update(same=0, diff=[])

    def put(table, key, value, label):
        if key in table:
            if table[key] == value:
                stat["same"] += 1
            else:
                stat["diff"].append((label, key))
            return
        table[key] = value

    for tag in tags or TAGS:
        base = raw_dir(tag)
        for it in insts:
            sym = it["symbol"]
            if it["market"] == "KRX":
                for r in json.loads((base / "daily_prices" / f"kr_{sym}_{tag}.json").read_text(encoding="utf-8")):
                    d = datetime.strptime(r["basDt"], "%Y%m%d").date()
                    put(prices, (sym, d), (_q(r["mkp"]), _q(r["hipr"]), _q(r["lopr"]), _q(r["clpr"]), int(r["trqu"])), "시세")
                continue
            for kind in ("false", "true"):
                path = base / "daily_prices" / f"us_{sym}_{tag}_adj{kind}.json"
                for r in json.loads(path.read_text(encoding="utf-8")).get("results") or []:
                    d = _et_date(r["t"])
                    if kind == "false":
                        v = int(Decimal(str(r["v"])).to_integral_value())
                        put(prices, (sym, d), (_q(r["o"]), _q(r["h"]), _q(r["l"]), _q(r["c"]), v), "시세")
                    else:
                        put(adj, (sym, d), _q(r["c"]), "수정 종가")
        payload = json.loads((base / "fx_rates" / f"USDKRW_{tag}.json").read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload["StatisticSearch"]["row"]
        for r in rows:
            put(fx, datetime.strptime(r["TIME"], "%Y%m%d").date(), _q(r["DATA_VALUE"]), "환율")
    return prices, adj, fx


def missing_raw(insts=None):
    """내보내기가 꼭 읽는 받은 원본(구간 목록의 모든 구간의 시세, 수정 종가, 환율, 기사와 일정 정리본) 가운데 없는 파일과
    꼭 읽는 파일의 수. 새로 받은 저장소에는 원본이 하나도 없습니다(재배포 금지, data/README.md)."""
    need = []
    for tag in TAGS:
        base = raw_dir(tag)
        for it in insts or instruments():
            sym = it["symbol"]
            if it["market"] == "KRX":
                need.append(base / "daily_prices" / f"kr_{sym}_{tag}.json")
            else:
                need += [base / "daily_prices" / f"us_{sym}_{tag}_adj{k}.json" for k in ("false", "true")]
        need += [base / "fx_rates" / f"USDKRW_{tag}.json", base / "news_massive" / f"ALL_{tag}.json"]
    need.append(EVENTS_FILE)
    return [p for p in need if not p.exists()], len(need)


def read_source():
    """원천 PostgreSQL의 시세와 환율(데이터 기간 안)과 기간 밖 행 수. VM과 원천 DB가 떠 있어야 합니다."""
    sys.path.insert(0, str(ROOT / "loaders"))
    from common import connect, load_env  # noqa: E402  적재기와 같은 접속(.env의 POSTGRES_*)
    with connect(load_env()) as con:
        prices = {(s, d): (_q(o), _q(h), _q(l), _q(c), int(v)) for s, d, o, h, l, c, v in con.execute(
            "select symbol, trade_date, open_price, high_price, low_price, close_price, volume "
            "from source.daily_prices where trade_date between %s and %s", (START, END)).fetchall()}
        fx = {d: _q(r) for d, r in con.execute(
            "select rate_date, rate from source.fx_rates where currency_pair = 'USDKRW' "
            "and rate_date between %s and %s", (START, END)).fetchall()}
        outside = con.execute("select (select count(*) from source.daily_prices where trade_date not between %s and %s)"
                              " + (select count(*) from source.fx_rates where currency_pair = 'USDKRW' "
                              "and rate_date not between %s and %s)", (START, END, START, END)).fetchone()[0]
    return prices, fx, outside


GOLD_PRICES = "lake.gold.mart_symbol_daily"
GOLD_FX = "lake.gold.mart_fx_daily"


def _tsv(v):
    """pyhive가 돌려준 값을 beeline tsv2가 찍는 글자와 같게 바꿉니다(두 길의 결과가 같도록)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):  # beeline은 소수 초 끝의 0을 지우되 한 자리는 남김(03.500000 → 03.5, 03.000000 → 03.0)
        s = v.isoformat(sep=" ", timespec="microseconds").rstrip("0")
        return s + "0" if s.endswith(".") else s
    return str(v)


def _spark_rows(sql):
    """Spark Thrift server에 SQL을 보내 결과 행(머리 뺌, 값은 모두 글자)을 돌려줍니다.
    - 내 Mac: infra/spark/sql.sh(spark 컨테이너 안의 beeline, tsv2). spark 컨테이너가 떠 있어야 합니다.
    - Airflow 컨테이너: 환경 변수 SPARK_THRIFT_HOST가 있으면 pyhive로 바로 붙습니다(컨테이너 안에는 docker가 없음).
      값은 beeline tsv2와 같은 글자로 바꿉니다(_tsv)."""
    import os
    if os.environ.get("SPARK_THRIFT_HOST"):
        from pyhive import hive
        conn = hive.connect(host=os.environ["SPARK_THRIFT_HOST"], port=int(os.environ.get("SPARK_THRIFT_PORT", "10000")),
                            username="screen_data", auth="NOSASL")
        try:
            cur = conn.cursor()
            cur.execute(sql)
            return [[_tsv(v) for v in r] for r in cur.fetchall()]
        finally:
            conn.close()
    p = subprocess.run([str(ROOT / "infra" / "spark" / "sql.sh"), "-e", sql], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "SQL_FORMAT": "tsv2"})
    if p.returncode != 0:
        sys.exit("Gold 마트를 읽지 못했습니다. docker compose --profile spark up -d로 Spark를 켜고, "
                 "docker compose --profile spark run --rm dbt build를 먼저 돌리세요.\n" + p.stderr[-800:])
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return [ln.split("\t") for ln in lines[1:]]


def read_gold():
    """Gold 마트의 시세와 환율(데이터 기간 안)과 두 마트의 마지막 스냅샷(id, 만든 때)."""
    from datetime import date
    prices = {(s, date.fromisoformat(d)): (_q(o), _q(h), _q(l), _q(c), int(v)) for s, d, o, h, l, c, v in _spark_rows(
        f"select symbol, trade_date, open_price, high_price, low_price, close_price, volume from {GOLD_PRICES} "
        f"where trade_date between '{START}' and '{END}'")}
    fx = {date.fromisoformat(d): _q(r) for d, r in _spark_rows(
        f"select rate_date, usdkrw_rate from {GOLD_FX} where rate_date between '{START}' and '{END}'")}
    snaps = {}
    for t in (GOLD_PRICES, GOLD_FX):
        rows = _spark_rows(f"select snapshot_id, committed_at from {t}.snapshots order by committed_at desc limit 1")
        snaps[t.rsplit(".", 1)[1]] = rows[0] if rows else None
    return prices, fx, snaps


def read_news(tags=None):
    """data/raw/의 전체 기사(E02 기준). 원천 DB에 넣지 않습니다(설계서 6절 6번). 구간마다 읽고, 같은 기사는
    앞 구간의 것을 남깁니다(두 구간에 걸친 날은 늦게 색인된 기사만 더해짐)."""
    out = {}
    for tag in tags or TAGS:
        payload = json.loads((raw_dir(tag) / "news_massive" / f"ALL_{tag}.json").read_text(encoding="utf-8"))
        part = {}  # 한 구간 안에서는 전처럼 뒤에 나온 것을 남김
        for page in payload["pages"]:
            for n in page.get("results") or []:
                part[n["id"]] = {"id": n["id"], "published_utc": n["published_utc"].replace("T", " ").rstrip("Z"),
                                 "title": n.get("title"), "description": n.get("description"),
                                 "publisher": (n.get("publisher") or {}).get("name"), "tickers": n.get("tickers") or [],
                                 "url": n.get("article_url")}
        for k, v in part.items():
            out.setdefault(k, v)
    return list(out.values())


def read_events():
    """data/raw/schedule/의 일정 정리본(loaders/schedule_events.py가 만듦). 원천 DB에 넣지 않습니다(기사와 같은 길).
    (일정 목록, FRED 일정을 받았는지). 정리본이 없으면 멈춥니다(일정 없이 7번 칸이 조용히 비지 않게)."""
    path = EVENTS_FILE
    if not path.exists():
        sys.exit(f"{path.relative_to(ROOT)}가 없습니다. 먼저 python loaders/schedule_events.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["events"], payload["fred"]


# ---------- 검사 ----------

def golden_sql():
    """손익 SQL(sql/pnl/)을 E01 골든 30개로 대조합니다(make check와 같은 스크립트)."""
    p = subprocess.run([sys.executable, str(ROOT / "eval" / "e01_pnl" / "check_sql.py"), "--no-save"],
                       cwd=ROOT, capture_output=True, text=True)
    m = re.search(r"숫자와 경고 일치 (\d+)/(\d+)", p.stdout)
    got = f"{m.group(1)}/{m.group(2)}" if m else "결과 없음"
    return p.returncode == 0 and bool(m) and m.group(1) == m.group(2), got


def _market_gaps(prices, insts):
    """시장마다 그 시장 종목들의 날짜를 합친 달력에서, 종목의 첫날과 마지막 날 사이에 빠진 날."""
    days = defaultdict(set)
    market = {it["symbol"]: it["market"] for it in insts}
    for s, d in prices:
        days[s].add(d)
    union = defaultdict(set)
    for s, ds in days.items():
        union[market[s]] |= ds
    gaps = {}
    for s, ds in days.items():
        lo, hi = min(ds), max(ds)
        miss = sorted(d for d in union[market[s]] if lo <= d <= hi and d not in ds)
        if miss:
            gaps[s] = miss
    return gaps


def check_real(insts, src_prices, src_fx, outside, raw_prices, raw_adj, raw_fx, golden=golden_sql, source=None, snaps=None,
               overlap=None, frozen=None):
    """10번 칸의 검사. (이름, 통과, 보일 값, 실패 때 내용) 목록을 돌려줍니다. 가격 값은 내용에 넣지 않습니다.
    src_prices, src_fx는 화면에 들어갈 값(Gold 마트)입니다. source=(원천 시세, 원천 환율)을 주면 레이크 경로 대조를 앞에 더하고,
    snaps를 주면 Gold 마트의 마지막 스냅샷을 보입니다. overlap(read_raw가 채움)을 주면 원본 구간끼리 겹치는 날을,
    frozen=(시세, 환율)(평가용 파일)을 주면 EVAL_END까지의 값이 평가용 파일과 같은지(늦게 나온 행을 더한 것만 허용) 봅니다."""
    out = []
    add = lambda name, ok, value, detail="": out.append((name, ok, value, detail))
    syms = [it["symbol"] for it in insts]
    count = lambda rows: {s: sum(1 for k in rows if k[0] == s) for s in syms}

    if source is not None:
        db_prices, db_fx = source
        miss = sorted(set(db_prices) - set(src_prices))
        extra = sorted(set(src_prices) - set(db_prices))
        diff = sorted(k for k in set(db_prices) & set(src_prices) if db_prices[k] != src_prices[k])
        n = len(miss) + len(extra) + len(diff)
        add("레이크 경로 시세(원천 → Bronze → Silver → Gold 한 행씩)", n == 0,
            f"{len(src_prices) - len(extra) - len(diff):,} / {len(db_prices):,}행 같음" + (f", {n}건 다름" if n else ""),
            "; ".join([f"{s} {d} Gold에 없음" for s, d in miss[:3]] + [f"{s} {d} 원천에 없음" for s, d in extra[:3]] +
                      [f"{s} {d} 값 다름" for s, d in diff[:3]]))
        miss = sorted(set(db_fx) - set(src_fx))
        extra = sorted(set(src_fx) - set(db_fx))
        diff = sorted(d for d in set(db_fx) & set(src_fx) if db_fx[d] != src_fx[d])
        n = len(miss) + len(extra) + len(diff)
        add("레이크 경로 환율(원천 → Gold 하루씩)", n == 0,
            f"{len(src_fx) - len(extra) - len(diff):,} / {len(db_fx):,}일 같음" + (f", {n}건 다름" if n else ""),
            "; ".join([f"{d} Gold에 없음" for d in miss[:3]] + [f"{d} 원천에 없음" for d in extra[:3]] +
                      [f"{d} 값 다름" for d in diff[:3]]))
    if snaps is not None:
        have = {k: v for k, v in snaps.items() if v}
        seoul = lambda t: (datetime.fromisoformat(t[:19]).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Seoul"))
                           .strftime("%Y-%m-%d %H:%M") if t[:4].isdigit() else t)  # Spark 세션 시간대는 UTC
        add("Gold 마트 스냅샷", len(have) == len(snaps),
            ", ".join(f"{k} {seoul(v[1])}" for k, v in have.items()) or "없음",
            "; ".join(f"{k} 스냅샷 {v[0]}" for k, v in have.items()) + ("" if len(have) == len(snaps) else "; 스냅샷 없는 마트 있음"))

    src_n, raw_n = count(src_prices), count(raw_prices)
    bad = [f"{s} {src_n[s]}/{raw_n[s]}" for s in syms if src_n[s] != raw_n[s]]
    add("Gold와 원본의 시세 행 수", not bad, f"{sum(src_n.values()):,} / {sum(raw_n.values()):,}",
        "다른 종목(Gold/원본): " + ", ".join(bad) if bad else "")

    missing = sorted(set(raw_prices) - set(src_prices))
    extra = sorted(set(src_prices) - set(raw_prices))
    names = ("시가", "고가", "저가", "종가", "거래량")
    diff = sorted(k for k in set(raw_prices) & set(src_prices) if raw_prices[k] != src_prices[k])
    show = [f"{s} {d} 빠짐" for s, d in missing[:3]] + [f"{s} {d} 원본에 없음" for s, d in extra[:3]] + \
           [f"{s} {d} " + ", ".join(n for n, a, b in zip(names, raw_prices[(s, d)], src_prices[(s, d)]) if a != b) +
            " 다름" for s, d in diff[:3]]
    bad_n = len(missing) + len(extra) + len(diff)
    add("시세 값 대조(Gold와 원본 한 행씩)", bad_n == 0,
        f"{len(src_prices) - len(extra) - len(diff):,}행 같음" + (f", {bad_n}건 다름" if bad_n else ""), "; ".join(show))

    bad, spans = [], []
    for s in syms:
        a = sorted(d for t, d in src_prices if t == s)
        b = sorted(d for t, d in raw_prices if t == s)
        if a and b and (a[0], a[-1]) == (b[0], b[-1]):
            spans.append((a[0], a[-1]))
        else:
            span = lambda x: f"{x[0]} ~ {x[-1]}" if x else "없음"
            bad.append(f"{s} Gold {span(a)}, 원본 {span(b)}")
    add("기간", not bad, f"{min(x[0] for x in spans)} ~ {max(x[1] for x in spans)}" if spans else "없음",
        "; ".join(bad))

    gaps, raw_gaps = _market_gaps(src_prices, insts), _market_gaps(raw_prices, insts)
    new = {s: [d for d in ds if d not in set(raw_gaps.get(s, []))] for s, ds in gaps.items()}
    new = {s: ds for s, ds in new.items() if ds}
    add("빈 거래일(시장 달력 기준)", not new, f"{sum(len(v) for v in gaps.values())}일",
        "; ".join(f"{s} {', '.join(map(str, ds[:3]))}" for s, ds in new.items()))

    fx_missing = sorted(set(raw_fx) - set(src_fx))
    fx_extra = sorted(set(src_fx) - set(raw_fx))
    fx_diff = sorted(d for d in set(raw_fx) & set(src_fx) if raw_fx[d] != src_fx[d])
    bad_n = len(fx_missing) + len(fx_extra) + len(fx_diff)
    add("환율 대조(Gold와 원본 하루씩)", bad_n == 0, f"{len(src_fx):,} / {len(raw_fx):,}일" + (f", {bad_n}건 다름" if bad_n else ""),
        "; ".join([f"{d} 빠짐" for d in fx_missing[:3]] + [f"{d} 원본에 없음" for d in fx_extra[:3]] +
                  [f"{d} 값 다름" for d in fx_diff[:3]]))

    us = {it["symbol"] for it in insts if it["currency"] == "USD"}
    us_days = sorted({d for s, d in src_prices if s in us})
    no_fx = [d for d in us_days if d not in src_fx]
    add("환율이 없는 미국 거래일", True, f"{len(no_fx)}일",
        "이 날의 매매는 직전 환율을 쓰고 FX_CARRIED 경고를 냄" if no_fx else "")

    # 분할은 원본의 두 판(수정 안 한 종가와 수정한 종가)의 비율로 봅니다. 원천 값의 잘못은 위의 값 대조가 잡습니다.
    splits, no_adj = [], []
    for s in sorted(us):
        prev = None
        for d in sorted(d for t, d in raw_prices if t == s):
            a = raw_adj.get((s, d))
            if a is None:
                no_adj.append(f"{s} {d}")
                continue
            ratio = raw_prices[(s, d)][3] / a
            if prev is not None and abs(ratio / prev - 1) > Decimal("0.01"):
                splits.append(f"{s} {d}")
            prev = ratio
    add("수정 가격(분할)", not splits and not no_adj, "분할 없음" if not splits else f"분할 후보 {len(splits)}건",
        "; ".join((["분할은 손익 SQL에 아직 넣지 않음: " + ", ".join(splits[:3])] if splits else []) +
                  (["수정 종가 없음: " + ", ".join(no_adj[:3])] if no_adj else [])))

    add("원천의 데이터 기간 밖 행", True, f"{outside}행", "내보내지 않음" if outside else "")

    if overlap is not None:
        diff = overlap["diff"]
        add("원본 구간이 겹치는 날(같은 값)", not diff, f"{overlap['same']:,}개 같음" + (f", {len(diff)}개 다름" if diff else ""),
            "; ".join(f"{label} {' '.join(map(str, k)) if isinstance(k, tuple) else k} 구간끼리 다름" for label, k in diff[:3]))
    if frozen is not None:
        from datetime import date
        cut = date.fromisoformat(EVAL_END)
        f_prices, f_fx = frozen
        bad, added, same = [], 0, 0
        for mine, theirs, label in ((src_prices, f_prices, "시세"), (src_fx, f_fx, "환율")):
            keys = {k for k in mine if (k[1] if isinstance(k, tuple) else k) <= cut}
            for k in sorted(set(theirs) - keys):
                bad.append(f"{label} {' '.join(map(str, k)) if isinstance(k, tuple) else k} 빠짐")
            for k in sorted(keys):
                if k not in theirs:
                    added += 1
                elif mine[k] != theirs[k]:
                    bad.append(f"{label} {' '.join(map(str, k)) if isinstance(k, tuple) else k} 값 다름")
                else:
                    same += 1
        add(f"평가용 파일과 같은 날({EVAL_END}까지)", not bad, f"{same:,}개 같음, 더한 행 {added}" + (f", {len(bad)}건 다름" if bad else ""),
            "; ".join(bad[:3]) or ("평가용 파일에 없던 행(늦게 나온 종가)" if added else ""))

    ok, got = golden()
    add("손익 SQL 골든", ok, got, "" if ok else "eval/e01_pnl/check_sql.py가 실패")
    return out


# ---------- 쓰기 ----------

def _create(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(SCHEMA)
    return con, tmp


def _insert_news(con, news):
    if not news:
        return
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "news.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for n in news:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        con.execute("insert into news select id, cast(published_utc as timestamp), title, description, publisher, "
                    "cast(tickers as varchar[]), url from read_json(?, format = 'newline_delimited', columns = "
                    "{id: 'varchar', published_utc: 'varchar', title: 'varchar', description: 'varchar', "
                    "publisher: 'varchar', tickers: 'varchar[]', url: 'varchar'})", [str(p)])


def fingerprint(con, tables=None):
    """내용 테이블, 검사 결과, meta(지문 칸 빼고)의 sha256. 내보낸 뒤 파일이 바뀌었는지(값, 검사 결과, 기준 종목)
    서버가 열 때 다시 셉니다. tables를 주면 그 테이블만 셉니다(content_digest)."""
    h = hashlib.sha256()
    for table, order in tables or CONTENT + [("checks", "seq"), ("meta where key <> 'fingerprint'", "key")]:
        h.update(table.encode())
        for row in con.execute(f"select * from {table} order by {order}").fetchall():
            h.update(repr(row).encode("utf-8"))
    return h.hexdigest()


def content_digest(path):
    """내용 테이블(시세, 환율, 달력, 기사, 일정, 종목)만의 sha256. 검사 결과와 내보낸 때는 넣지 않습니다.
    Airflow가 30분마다 내보낼 때, 내용이 같으면 화면 파일을 바꾸지 않으려고 씁니다."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        return fingerprint(con, CONTENT)
    finally:
        con.close()


def _finish(con, tmp, path, meta, checks):
    try:
        con.executemany("insert into checks values (?, ?, ?, ?, ?)", [[i, *c] for i, c in enumerate(checks)])
        meta = {**meta, "exported_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")}  # Airflow 컨테이너는 UTC
        con.executemany("insert into meta values (?, ?)", [[k, str(v)] for k, v in meta.items()])
        con.execute("insert into meta values ('fingerprint', ?)", [fingerprint(con)])
    finally:
        con.close()
    tmp.replace(path)


def write_real(path, insts, src_prices, src_fx, raw_adj, news, checks, events=((), False), source_label=None,
               window=None):
    con, tmp = _create(path)
    events, fred = events
    try:
        _fill_real(con, insts, src_prices, src_fx, raw_adj, news)
        if events:  # 자체 시험과 파이프라인 시험은 일정 없이 만듦(DuckDB executemany는 빈 목록을 받지 않음)
            con.executemany("insert into events values (?, ?, ?, ?, ?, ?, ?)",
                            [[e["id"], e["date"], e["kind"], e["title"], e["source"], e["local_time"], e["tz"]]
                             for e in events])
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    start, end = window or (START, END)
    meta = {"kind": "real", "bench": REAL_BENCH, "window_start": start, "window_end": end,
            "source": source_label or "받은 원본(자체 시험). 수정 종가와 기사는 data/raw/",
            "rows_prices": len(src_prices), "rows_fx": len(src_fx), "rows_news": len(news),
            "rows_events": len(events), "events_fred": fred}
    _finish(con, tmp, path, meta, checks)


def _fill_real(con, insts, src_prices, src_fx, raw_adj, news):
    con.executemany("insert into instruments values (?, ?, ?, ?, ?, ?)",
                    [[i["symbol"], i["name"], i["market"], i["currency"], i["asset_class"], i["grp"]] for i in insts])
    kr = {i["symbol"] for i in insts if i["currency"] == "KRW"}
    con.executemany("insert into prices values (?, ?, ?, ?, ?, ?, ?, ?)",
                    [[s, d, o, h, l, c, c if s in kr else raw_adj.get((s, d)), v]
                     for (s, d), (o, h, l, c, v) in sorted(src_prices.items())])
    con.executemany("insert into fx values (?, ?)", sorted(src_fx.items()))
    days = sorted({d for _, d in src_prices} | set(src_fx))
    con.executemany("insert into calendar values (?)", [[d] for d in days])
    _insert_news(con, news)


def export_if_changed(path=OUT):
    """내보내되, 새 파일과 지금 파일의 내용 테이블이 같고 둘 다 검사를 통과했으면 지금 파일을 그대로 둡니다
    (Airflow DAG lakehouse_refresh의 screen_export). 같은 입력이면 파일이 바뀌지 않아, 켜 둔 화면 서버가 읽는 파일도 그대로입니다.
    새 파일의 검사가 실패하면 내용이 같아도 바꿉니다(화면이 실패 항목을 보이게). 돌려주는 값: (파일, 바꿨나)."""
    if not path.exists():
        return export(path), True
    new = export(path.with_name(path.stem + ".new.duckdb"))
    same = content_digest(new) == content_digest(path)
    if same and status(new)["ok"] and status(path)["ok"]:
        new.unlink()
        return path, False
    new.replace(path)
    return path, True


def read_frozen(path=EVAL):
    """평가용 파일의 시세와 환율(검사 모양). 파일이 없으면 None(대조를 건너뜀, 새로 받은 저장소)."""
    if not Path(path).exists():
        return None
    con = duckdb.connect(str(path), read_only=True)
    try:
        prices = {(s, d): (_q(o), _q(h), _q(l), _q(c), int(v)) for s, d, o, h, l, c, v in con.execute(
            "select symbol, trade_date, open, high, low, close, volume from prices").fetchall()}
        fx = {d: _q(r) for d, r in con.execute("select rate_date, rate from fx").fetchall()}
    finally:
        con.close()
    return prices, fx


def eval_db():
    """평가 시험이 여는 화면 파일(얼린 사본). 없거나 내용 지문이 고정한 값과 다르면 멈춥니다(조용히 늘린 파일을 쓰지 않게)."""
    if not EVAL.exists():
        sys.exit(f"{EVAL.relative_to(ROOT)}가 없습니다. .venv/bin/python -m ai.screen_data freeze 로 첫 구간 원본에서 다시 만듭니다")
    global _EVAL_OK
    if not _EVAL_OK:
        got = content_digest(EVAL)
        if got != EVAL_DIGEST:
            sys.exit(f"{EVAL.relative_to(ROOT)}의 내용이 정답을 고정한 때와 다릅니다(지문 {got[:12]}, 기대 {EVAL_DIGEST[:12]})")
        _EVAL_OK = True
    return EVAL


_EVAL_OK = False


def freeze(path=EVAL):
    """평가용 파일을 첫 구간 원본(data/raw/, 2024-11-13 ~ 2026-11-12)에서 다시 만들고 지문을 대조합니다.
    내보낸 화면 파일은 Gold와 원본을 한 행씩 대조해 같았으므로, 원본으로 만든 내용이 그때의 화면 파일과 같아야 합니다."""
    insts = instruments()
    tag = BASE_TAG
    raw_prices, raw_adj, raw_fx = read_raw(insts, [tag])
    checks = check_real(insts, raw_prices, raw_fx, 0, raw_prices, raw_adj, raw_fx)
    new = path.with_name(path.stem + ".new.duckdb")
    write_real(new, insts, raw_prices, raw_fx, raw_adj, read_news([tag]), checks, read_events(),
               source_label="평가용(첫 구간 원본에서 다시 만듦)", window=BASE)
    got = content_digest(new)
    if got != EVAL_DIGEST:  # 지금 평가용 파일은 건드리지 않고, 새 파일은 살펴보도록 남김
        sys.exit(f"내용 지문이 다릅니다: {got[:12]} (기대 {EVAL_DIGEST[:12]}). {new.relative_to(ROOT)}를 남겼습니다")
    new.replace(path)
    print(f"내용 지문 같음: {got[:12]}")
    return path


def export(path=OUT):
    insts = instruments()
    gold_prices, gold_fx, snaps = read_gold()
    db_prices, db_fx, outside = read_source()
    overlap = {}
    raw_prices, raw_adj, raw_fx = read_raw(insts, overlap=overlap)
    checks = check_real(insts, gold_prices, gold_fx, outside, raw_prices, raw_adj, raw_fx,
                        source=(db_prices, db_fx), snaps=snaps, overlap=overlap, frozen=read_frozen())
    label = (f"Gold 마트 {GOLD_PRICES}, {GOLD_FX}(원천 PostgreSQL → Debezium, Kafka → Flink Bronze → Spark Silver → dbt Gold). "
             "수정 종가와 기사는 data/raw/")
    write_real(path, insts, gold_prices, gold_fx, raw_adj, read_news(), checks, read_events(), source_label=label)
    return path


def golden(path=GOLDEN_OUT):
    """E01 골든의 가짜 시세(fixture.json)를 화면 파일과 같은 구조로. 달력은 fixture의 것을 그대로 씁니다."""
    fix = json.loads((ROOT / "eval" / "e01_pnl" / "fixture.json").read_text(encoding="utf-8"))
    con, tmp = _create(path)
    try:
        for s, v in fix["symbols"].items():
            con.execute("insert into instruments values (?, ?, ?, ?, '골든', '골든')",
                        [s, s, "KRX" if v["currency"] == "KRW" else "US", v["currency"]])
            con.executemany("insert into prices values (?, ?, null, ?, ?, ?, ?, null)",
                            [[s, d, _q(r["high"]), _q(r["low"]), _q(r["close"]), _q(r["close"])]
                             for d, r in sorted(v["rows"].items())])
        con.executemany("insert into fx values (?, ?)", [[d, _q(r)] for d, r in sorted(fix["usdkrw"].items())])
        con.executemany("insert into calendar values (?)", [[d] for d in fix["calendar"]])
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    ok, got = golden_sql()
    checks = [("골든용 가짜 시세", True, f"종목 {len(fix['symbols'])}개, 달력 {len(fix['calendar'])}일",
               "eval/e01_pnl/fixture.json. 원본 대조 없음"),
              ("손익 SQL 골든", ok, got, "" if ok else "eval/e01_pnl/check_sql.py가 실패")]
    _finish(con, tmp, path, {"kind": "golden", "bench": "BENCH", "source": "eval/e01_pnl/fixture.json"}, checks)
    return path


# ---------- 읽어 쓰기(서버와 도구 층) ----------

MISSING = {"ok": False, "checks": [], "failures": [{"name": "화면 데이터 파일", "ok": False, "value": "없음",
                                                     "detail": "make screen-data로 내보냄"}]}


def status_of(con):
    """열린 연결로 저장한 검사와 지문을 대조합니다. ok가 거짓이면 화면은 숫자 대신 failures를 보입니다."""
    meta = dict(con.execute("select key, value from meta").fetchall())
    checks = [{"name": n, "ok": ok, "value": v, "detail": dt} for n, ok, v, dt in
              con.execute("select name, ok, value, detail from checks order by seq").fetchall()]
    same = fingerprint(con) == meta.get("fingerprint")
    checks.append({"name": "내보낸 뒤 바뀌지 않음(지문)", "ok": same, "value": "같음" if same else "다름",
                   "detail": "" if same else "내보낸 뒤 파일이 바뀜. 다시 내보냄"})
    fails = [c for c in checks if not c["ok"]]
    return {"ok": not fails, "kind": meta.get("kind"), "exported_at": meta.get("exported_at"), "source": meta.get("source"),
            "window": [meta.get("window_start"), meta.get("window_end")], "bench": meta.get("bench"),
            "checks": checks, "failures": fails}


def status(path=OUT):
    if not Path(path).exists():
        return dict(MISSING)
    con = duckdb.connect(str(path), read_only=True)
    try:
        return status_of(con)
    finally:
        con.close()


def load_world(path=OUT):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return world_of(con)
    finally:
        con.close()


def world_of(con):
    """손익 엔진과 도구 층이 쓰는 모양: calendar, symbols{종목: name, market, currency, rows{날짜: open, high, low,
    close, adj_close, volume}}, usdkrw{날짜: 환율}, bench, kind. 날짜는 'YYYY-MM-DD' 문자열입니다."""
    meta = dict(con.execute("select key, value from meta").fetchall())
    symbols = {s: {"name": n, "market": m, "currency": c, "rows": {}} for s, n, m, c in
               con.execute("select symbol, name, market, currency from instruments order by symbol").fetchall()}
    for s, d, o, h, l, c, a, v in con.execute("select * from prices order by symbol, trade_date").fetchall():
        symbols[s]["rows"][d.isoformat()] = {"open": o, "high": h, "low": l, "close": c, "adj_close": a, "volume": v}
    world = {"kind": meta["kind"], "bench": meta["bench"], "symbols": symbols,
             "calendar": [d.isoformat() for (d,) in con.execute("select cal_date from calendar order by 1").fetchall()],
             "usdkrw": {d.isoformat(): r for d, r in con.execute("select rate_date, rate from fx order by 1").fetchall()}}
    return world


# ---------- 자체 시험 ----------

def selftest():
    """VM 없이 원본을 Gold와 원천 대신 넣어(같으면 모든 검사 통과) Gold 쪽 값을 세 가지로 깨고, 내보낸 뒤 파일도 고쳐 봅니다.
    원천은 원본 그대로라, Gold를 깨면 레이크 경로 대조와 원본 대조가 함께 실패해야 합니다."""
    insts = instruments()
    raw_prices, raw_adj, raw_fx = read_raw(insts)
    no_golden = lambda: (True, "30/30(자체 시험에서는 건너뜀)")
    rng = random.Random(20261116)
    results = []

    def run(name, prices, fx, want, **kw):
        checks = check_real(insts, prices, fx, 0, raw_prices, raw_adj, raw_fx, golden=no_golden,
                            source=(raw_prices, raw_fx), snaps={"mart_symbol_daily": ["1", "자체 시험"], "mart_fx_daily": ["2", "자체 시험"]},
                            **kw)
        failed = [c[0] for c in checks if not c[1]]
        ok = failed == want
        results.append((name, ok, failed))

    run("원본 그대로", dict(raw_prices), dict(raw_fx), [])
    keys = sorted(raw_prices)
    k = rng.choice(keys)
    broken = dict(raw_prices)
    del broken[k]
    LP, LF = "레이크 경로 시세(원천 → Bronze → Silver → Gold 한 행씩)", "레이크 경로 환율(원천 → Gold 하루씩)"
    run("시세 행 하나 지우기", broken, dict(raw_fx), [LP, "Gold와 원본의 시세 행 수", "시세 값 대조(Gold와 원본 한 행씩)", "기간"
                                               if k in _ends(raw_prices) else "빈 거래일(시장 달력 기준)"])
    d = rng.choice(sorted(raw_fx))
    fx = dict(raw_fx)
    del fx[d]
    run("환율 하루 빼기", dict(raw_prices), fx, [LF, "환율 대조(Gold와 원본 하루씩)"])
    k = rng.choice(keys)
    broken = dict(raw_prices)
    o, h, l, c, v = broken[k]
    broken[k] = (o, h, l, (c * Decimal("1.01")).quantize(Q4), v)
    run("종가 하나 바꾸기", broken, dict(raw_fx), [LP, "시세 값 대조(Gold와 원본 한 행씩)"])

    # 원천만 다르고 Gold는 원본과 같은 경우(레이크 경로가 원천의 변경을 아직 반영하지 않음)는 레이크 경로 대조만 잡습니다
    k = rng.choice(keys)
    db = dict(raw_prices)
    o, h, l, c, v = db[k]
    db[k] = (o, h, l, (c * Decimal("0.99")).quantize(Q4), v)
    checks = check_real(insts, dict(raw_prices), dict(raw_fx), 0, raw_prices, raw_adj, raw_fx, golden=no_golden,
                        source=(db, dict(raw_fx)), snaps={"mart_symbol_daily": ["1", "자체 시험"], "mart_fx_daily": ["2", "자체 시험"]})
    failed = [c[0] for c in checks if not c[1]]
    results.append(("원천만 바뀌고 Gold는 그대로", failed == [LP], failed))

    # 원본 구간(구간 목록)과 평가용 파일 대조. 평가용 파일은 EVAL_END까지의 원본으로 꾸밉니다
    from datetime import date
    cut = date.fromisoformat(EVAL_END)
    old_keys = sorted(k for k in keys if k[1] <= cut)
    f_prices = {k: raw_prices[k] for k in old_keys}
    f_fx = {d: r for d, r in raw_fx.items() if d <= cut}
    OV, FZ = "원본 구간이 겹치는 날(같은 값)", f"평가용 파일과 같은 날({EVAL_END}까지)"
    run("구간과 평가용 파일 그대로", dict(raw_prices), dict(raw_fx), [], overlap={"same": 3, "diff": []},
        frozen=(f_prices, f_fx))
    k = rng.choice(old_keys)
    run("겹치는 날 원본끼리 다름", dict(raw_prices), dict(raw_fx), [OV], overlap={"same": 2, "diff": [("시세", k)]})
    changed = dict(f_prices)
    o, h, l, c, v = changed[k]
    changed[k] = (o, h, l, (c * Decimal("1.01")).quantize(Q4), v)
    run("평가용 파일 때의 종가가 바뀜", dict(raw_prices), dict(raw_fx), [FZ], frozen=(changed, f_fx))
    fewer = dict(f_prices)
    del fewer[k]
    d = rng.choice(sorted(f_fx))
    run("평가용 파일에 없던 행을 더함(늦게 나온 종가)", dict(raw_prices), dict(raw_fx), [],
        frozen=(fewer, {x: r for x, r in f_fx.items() if x != d}))

    tampers = [("내보낸 뒤 행 지우기",
                "delete from prices where (symbol, trade_date) in (select symbol, trade_date from prices limit 1)"),
               ("내보낸 뒤 검사 결과 고치기", "update checks set value = '고침' where seq = 0"),
               ("내보낸 뒤 기준 종목 바꾸기", "update meta set value = 'QQQ' where key = 'bench'")]
    with tempfile.TemporaryDirectory() as tmpd:
        checks = check_real(insts, raw_prices, raw_fx, 0, raw_prices, raw_adj, raw_fx, golden=no_golden)
        for i, (name, sql) in enumerate(tampers):
            path = Path(tmpd) / f"screen_{i}.duckdb"
            write_real(path, insts, raw_prices, raw_fx, raw_adj, [], checks)
            before = status(path)["ok"]
            con = duckdb.connect(str(path))
            con.execute(sql)
            con.close()
            after = status(path)
            results.append((name, before and not after["ok"] and
                            [f["name"] for f in after["failures"]] == ["내보낸 뒤 바뀌지 않음(지문)"],
                            [f["name"] for f in after["failures"]]))
    for name, ok, failed in results:
        print(f"{'통과' if ok else '실패'}  {name}: 실패한 검사 {failed or '없음'}")
    good = all(ok for _, ok, _ in results)
    print("자체 시험:", "통과" if good else "실패")
    return 0 if good else 1


def _ends(prices):
    by = defaultdict(list)
    for s, d in prices:
        by[s].append(d)
    return {(s, d) for s, ds in by.items() for d in (min(ds), max(ds))}


def _print_status(st):
    print(f"종류 {st.get('kind')}, 내보낸 때 {st.get('exported_at')}, 판정 {'통과' if st['ok'] else '실패'}")
    for c in st["checks"]:
        print(f"  {'통과' if c['ok'] else '실패'}  {c['name']}: {c['value']}" + (f"  ({c['detail']})" if c["detail"] else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["export", "golden", "status", "freeze"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--if-changed", action="store_true", help="export: 내용이 같고 검사가 통과하면 지금 파일을 그대로 둠")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.cmd == "export" and a.if_changed:
        miss, need = missing_raw()
        if miss and len(miss) == need:  # 새로 받은 저장소: DAG의 앞 태스크(Silver, dbt)는 돌고 화면 파일만 건너뜀
            print(f"받은 원본이 없어 화면 파일 내보내기를 건너뜁니다(없는 파일 {len(miss)}개, 예: "
                  f"{miss[0].relative_to(ROOT)}). 원본 받기는 loaders/README.md")
            sys.exit(SKIP)
        if miss:  # 일부만 없음: 건너뛰면 화면 파일이 예전 그대로 남고 실행은 성공으로 보임(2026-11-24 재검토). 실패로 알림
            sys.exit(f"받은 원본 {need}개 가운데 {len(miss)}개가 없습니다(예: "
                     + ", ".join(str(m.relative_to(ROOT)) for m in miss[:3]) + "). 원본을 다시 받은 뒤 내보냅니다")
        path, changed = export_if_changed(Path(a.path) if a.path else OUT)
        st = status(path)
        print("화면 파일을 새로 씀" if changed else
              f"내용이 같아 화면 파일을 그대로 둠(내보낸 때 {st.get('exported_at')}). 이번 대조는 모두 통과")
    elif a.cmd == "export":
        st = status(export(Path(a.path) if a.path else OUT))
    elif a.cmd == "golden":
        st = status(golden(Path(a.path) if a.path else GOLDEN_OUT))
    elif a.cmd == "status":
        st = status(Path(a.path) if a.path else OUT)
    elif a.cmd == "freeze":
        st = status(freeze())
    else:
        sys.exit(__doc__)
    _print_status(st)
    sys.exit(0 if st["ok"] else 1)
