"""화면 데이터 파일을 만들고 검사합니다(docs/ai/F-screen.md 2절 「구조」, 화면 10번 칸 「파이프라인 상태」).

사용: .venv/bin/python -m ai.screen_data export        원천 PostgreSQL(VM 필요)과 data/raw/에서 내보내고 검사
      .venv/bin/python -m ai.screen_data golden        E01 골든의 가짜 시세로 같은 구조의 파일(골든 대조용)
      .venv/bin/python -m ai.screen_data status [파일]  저장한 검사 결과와 지문 대조
      .venv/bin/python -m ai.screen_data --selftest    일부러 깬 데이터를 잡는지(행 지우기, 환율 하루 빼기, 종가 바꾸기,
                                                       내보낸 뒤 파일 고치기). VM 없이 원본을 원천 대신 씀

- 시세와 환율은 원천 DB에서 읽고, 내보낼 때 받은 원본(data/raw/)과 한 행씩 대조합니다. 원천이 원본과 다르면
  (적재 실수, 빠진 행, 바뀐 값) 검사가 실패합니다. 원본 읽기는 독립 정답(eval/f_screen/real_world.py)과 공유하지 않습니다.
- 수정한 가격(adj_close)과 기사는 원천 DB에 없어(설계서 6절 2, 6번) data/raw/에서 바로 넣습니다.
- 검사 결과는 파일의 checks 테이블에, 내용의 지문은 meta에 둡니다. 서버는 열 때 지문을 다시 셉니다.
  검사가 하나라도 실패하거나 지문이 다르면 화면은 숫자 대신 실패 항목을 보입니다.
- 가격 값은 출력하지 않습니다(재배포 금지). 행 수, 날짜, 개수만 냅니다.
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
START, END = "2024-11-13", "2026-11-12"
TAG = START.replace("-", "") + "_" + END.replace("-", "")
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
create table events (id varchar primary key, event_date date, kind varchar, title varchar, source varchar);
create table checks (seq integer, name varchar, ok boolean, value varchar, detail varchar);
"""
CONTENT = [("instruments", "symbol"), ("prices", "symbol, trade_date"), ("fx", "rate_date"),
           ("calendar", "cal_date"), ("news", "id"), ("events", "id")]


# ---------- 읽기 ----------

def instruments():
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8", newline="") as f:
        return [{"symbol": r["symbol"], "name": r["name"], "market": r["market"], "currency": r["currency"],
                 "asset_class": r["asset_class"], "grp": r["group"]} for r in csv.DictReader(f)]


def _q(v):
    return Decimal(str(v)).quantize(Q4)


def _et_date(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET).date()


def read_raw(insts):
    """받은 원본에서 시세(시가, 고가, 저가, 종가, 거래량), 수정 종가, 환율을 읽습니다. 키는 (종목, date)."""
    prices, adj = {}, {}
    for it in insts:
        sym = it["symbol"]
        if it["market"] == "KRX":
            for r in json.loads((RAW / "daily_prices" / f"kr_{sym}_{TAG}.json").read_text(encoding="utf-8")):
                d = datetime.strptime(r["basDt"], "%Y%m%d").date()
                prices[(sym, d)] = (_q(r["mkp"]), _q(r["hipr"]), _q(r["lopr"]), _q(r["clpr"]), int(r["trqu"]))
            continue
        for kind in ("false", "true"):
            path = RAW / "daily_prices" / f"us_{sym}_{TAG}_adj{kind}.json"
            for r in json.loads(path.read_text(encoding="utf-8"))["results"]:
                d = _et_date(r["t"])
                if kind == "false":
                    v = int(Decimal(str(r["v"])).to_integral_value())
                    prices[(sym, d)] = (_q(r["o"]), _q(r["h"]), _q(r["l"]), _q(r["c"]), v)
                else:
                    adj[(sym, d)] = _q(r["c"])
    payload = json.loads((RAW / "fx_rates" / f"USDKRW_{TAG}.json").read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload["StatisticSearch"]["row"]
    fx = {datetime.strptime(r["TIME"], "%Y%m%d").date(): _q(r["DATA_VALUE"]) for r in rows}
    return prices, adj, fx


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


def read_news():
    """data/raw/의 2년 전체 기사(E02 기준). 원천 DB에 넣지 않습니다(설계서 6절 6번)."""
    payload = json.loads((RAW / "news_massive" / f"ALL_{TAG}.json").read_text(encoding="utf-8"))
    out = {}
    for page in payload["pages"]:
        for n in page["results"]:
            out[n["id"]] = {"id": n["id"], "published_utc": n["published_utc"].replace("T", " ").rstrip("Z"),
                            "title": n.get("title"), "description": n.get("description"),
                            "publisher": (n.get("publisher") or {}).get("name"), "tickers": n.get("tickers") or [],
                            "url": n.get("article_url")}
    return list(out.values())


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


def check_real(insts, src_prices, src_fx, outside, raw_prices, raw_adj, raw_fx, golden=golden_sql):
    """10번 칸의 검사. (이름, 통과, 보일 값, 실패 때 내용) 목록을 돌려줍니다. 가격 값은 내용에 넣지 않습니다."""
    out = []
    add = lambda name, ok, value, detail="": out.append((name, ok, value, detail))
    syms = [it["symbol"] for it in insts]
    count = lambda rows: {s: sum(1 for k in rows if k[0] == s) for s in syms}

    src_n, raw_n = count(src_prices), count(raw_prices)
    bad = [f"{s} {src_n[s]}/{raw_n[s]}" for s in syms if src_n[s] != raw_n[s]]
    add("원천과 원본의 시세 행 수", not bad, f"{sum(src_n.values()):,} / {sum(raw_n.values()):,}",
        "다른 종목(원천/원본): " + ", ".join(bad) if bad else "")

    missing = sorted(set(raw_prices) - set(src_prices))
    extra = sorted(set(src_prices) - set(raw_prices))
    names = ("시가", "고가", "저가", "종가", "거래량")
    diff = sorted(k for k in set(raw_prices) & set(src_prices) if raw_prices[k] != src_prices[k])
    show = [f"{s} {d} 빠짐" for s, d in missing[:3]] + [f"{s} {d} 원본에 없음" for s, d in extra[:3]] + \
           [f"{s} {d} " + ", ".join(n for n, a, b in zip(names, raw_prices[(s, d)], src_prices[(s, d)]) if a != b) +
            " 다름" for s, d in diff[:3]]
    bad_n = len(missing) + len(extra) + len(diff)
    add("시세 값 대조(원본과 한 행씩)", bad_n == 0,
        f"{len(src_prices) - len(extra) - len(diff):,}행 같음" + (f", {bad_n}건 다름" if bad_n else ""), "; ".join(show))

    bad, spans = [], []
    for s in syms:
        a = sorted(d for t, d in src_prices if t == s)
        b = sorted(d for t, d in raw_prices if t == s)
        if a and b and (a[0], a[-1]) == (b[0], b[-1]):
            spans.append((a[0], a[-1]))
        else:
            span = lambda x: f"{x[0]} ~ {x[-1]}" if x else "없음"
            bad.append(f"{s} 원천 {span(a)}, 원본 {span(b)}")
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
    add("환율 대조(원본과 하루씩)", bad_n == 0, f"{len(src_fx):,} / {len(raw_fx):,}일" + (f", {bad_n}건 다름" if bad_n else ""),
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


def fingerprint(con):
    """내용 테이블, 검사 결과, meta(지문 칸 빼고)의 sha256. 내보낸 뒤 파일이 바뀌었는지(값, 검사 결과, 기준 종목)
    서버가 열 때 다시 셉니다."""
    h = hashlib.sha256()
    for table, order in CONTENT + [("checks", "seq"), ("meta where key <> 'fingerprint'", "key")]:
        h.update(table.encode())
        for row in con.execute(f"select * from {table} order by {order}").fetchall():
            h.update(repr(row).encode("utf-8"))
    return h.hexdigest()


def _finish(con, tmp, path, meta, checks):
    try:
        con.executemany("insert into checks values (?, ?, ?, ?, ?)", [[i, *c] for i, c in enumerate(checks)])
        meta = {**meta, "exported_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        con.executemany("insert into meta values (?, ?)", [[k, str(v)] for k, v in meta.items()])
        con.execute("insert into meta values ('fingerprint', ?)", [fingerprint(con)])
    finally:
        con.close()
    tmp.replace(path)


def write_real(path, insts, src_prices, src_fx, raw_adj, news, checks):
    con, tmp = _create(path)
    try:
        _fill_real(con, insts, src_prices, src_fx, raw_adj, news)
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    meta = {"kind": "real", "bench": REAL_BENCH, "window_start": START, "window_end": END,
            "source": "원천 PostgreSQL source.daily_prices, source.fx_rates(USDKRW). 수정 종가와 기사는 data/raw/",
            "rows_prices": len(src_prices), "rows_fx": len(src_fx), "rows_news": len(news)}
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


def export(path=OUT):
    insts = instruments()
    src_prices, src_fx, outside = read_source()
    raw_prices, raw_adj, raw_fx = read_raw(insts)
    checks = check_real(insts, src_prices, src_fx, outside, raw_prices, raw_adj, raw_fx)
    write_real(path, insts, src_prices, src_fx, raw_adj, read_news(), checks)
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
    return {"ok": not fails, "kind": meta.get("kind"), "exported_at": meta.get("exported_at"),
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
    """VM 없이 원본을 원천 대신 넣어(같으면 모든 검사 통과) 세 가지를 깨고, 내보낸 뒤 파일도 고쳐 봅니다."""
    insts = instruments()
    raw_prices, raw_adj, raw_fx = read_raw(insts)
    no_golden = lambda: (True, "30/30(자체 시험에서는 건너뜀)")
    rng = random.Random(20261116)
    results = []

    def run(name, prices, fx, want):
        checks = check_real(insts, prices, fx, 0, raw_prices, raw_adj, raw_fx, golden=no_golden)
        failed = [c[0] for c in checks if not c[1]]
        ok = failed == want
        results.append((name, ok, failed))

    run("원본 그대로", dict(raw_prices), dict(raw_fx), [])
    keys = sorted(raw_prices)
    k = rng.choice(keys)
    broken = dict(raw_prices)
    del broken[k]
    run("시세 행 하나 지우기", broken, dict(raw_fx), ["원천과 원본의 시세 행 수", "시세 값 대조(원본과 한 행씩)", "기간"
                                               if k in _ends(raw_prices) else "빈 거래일(시장 달력 기준)"])
    d = rng.choice(sorted(raw_fx))
    fx = dict(raw_fx)
    del fx[d]
    run("환율 하루 빼기", dict(raw_prices), fx, ["환율 대조(원본과 하루씩)"])
    k = rng.choice(keys)
    broken = dict(raw_prices)
    o, h, l, c, v = broken[k]
    broken[k] = (o, h, l, (c * Decimal("1.01")).quantize(Q4), v)
    run("종가 하나 바꾸기", broken, dict(raw_fx), ["시세 값 대조(원본과 한 행씩)"])

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
    ap.add_argument("cmd", nargs="?", choices=["export", "golden", "status"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.cmd == "export":
        st = status(export(Path(a.path) if a.path else OUT))
    elif a.cmd == "golden":
        st = status(golden(Path(a.path) if a.path else GOLDEN_OUT))
    elif a.cmd == "status":
        st = status(Path(a.path) if a.path else OUT)
    else:
        sys.exit(__doc__)
    _print_status(st)
    sys.exit(0 if st["ok"] else 1)
