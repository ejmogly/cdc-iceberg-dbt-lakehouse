#!/usr/bin/env python3
"""화면 데이터 기간 늘리기. 받기는 Mac이, 받은 원본의 처리는 Airflow DAG window_extend가 합니다(docs/decisions/0016).

호스트(키가 .env에 있는 곳. 레이크가 꺼져 있어도 됨):
  .venv/bin/python loaders/extend_window.py fetch [--end YYYY-MM-DD] [--plan]
        국내 시세가 나온 마지막 거래일까지 새 구간 원본 38개를 data/raw/window/에 받고, 다 받은 뒤에
        data/raw/window/next.json(구간과 파일 지문)을 적습니다. 새로 받을 날이 없으면 아무것도 하지 않습니다.
Airflow window_extend(컨테이너에는 data/raw/가 읽기 전용, data/generated/가 쓰기로 붙음. 키와 인터넷이 필요 없음):
  pending              받아 둔 새 원본이 있으면 0, 아직 없으면 99(센서가 다시 봄), 앞뒤가 안 맞으면 2
  check                next.json의 지문, 국내 끝날, 앞 구간과 겹치는 날의 값을 대조. 마지막 줄 「시작 끝」(XCom)
  load 시작 끝         같은 원본으로 원천 적재 셋(--from-raw). 마지막 줄은 바뀐 행 수와 lsn 기준점(JSON, XCom)
  wait-bronze JSON     바뀐 행이 모두 Bronze에 닿았으면 0, 아직이면 99(Flink checkpoint를 기다림)
  add 시작 끝          data/generated/window.json에 구간을 더함(이미 있으면 그대로). 이 뒤의 새로 고침부터 화면 기간이 늚
  confirm 끝           화면 파일의 기간 끝이 그날이고 검사가 모두 통과했는지
  --selftest           VM, 키, 원본 없이 끝날 고르기(주말과 휴장), 처리할 것 고르기, 지문, 구간 더하기, 바뀐 행 세기를 시험

- 새 구간은 지금 마지막 구간의 끝날부터 하루 겹칩니다. 받을 때 아직 나오지 않았던 미국 종가를 다음에 채우려는 것이고,
  겹친 날은 원본끼리 값이 같아야 합니다(check와 내보내기 검사 「원본 구간이 겹치는 날」).
- 끝날은 「어제」가 아니라 국내 시세가 실제로 나온 마지막 거래일입니다. 국내 시세는 다음 영업일 오후 1시 뒤에 나오므로,
  주말이나 월요일 오전에 어제를 끝날로 잡으면 금요일 국내 시세 없이 구간이 더해지고 다시 받지 않게 됩니다(11/19에 찾음).
- 가격 값은 출력하지 않습니다. 행 수와 날짜만 냅니다.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
RAW_WINDOW_REL = "data/raw/window"          # 적재기의 LOADER_RAW_DIR(loaders/common.py)
RAW_WINDOW = ROOT / RAW_WINDOW_REL
NEXT = RAW_WINDOW / "next.json"
SEOUL = ZoneInfo("Asia/Seoul")
PY = sys.executable
NOT_YET = 99                                 # Airflow BashSensor의 retry_exit_code


def symbols():
    import csv
    with open(ROOT / "loaders" / "instruments.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r["symbol"] for r in rows if r["market"] == "KRX"], [r["symbol"] for r in rows if r["market"] == "US"]


def tag_of(start, end):
    return f"{start.replace('-', '')}_{end.replace('-', '')}"


def files(tag, kr=None, us=None):
    """한 구간의 원본 파일(data/raw/window/ 기준 상대 경로). 국내 2, 미국 17 x 두 판, 환율 1, 기사 1 = 38개."""
    if kr is None or us is None:
        kr, us = symbols()
    out = [f"daily_prices/kr_{s}_{tag}.json" for s in kr]
    out += [f"daily_prices/us_{s}_{tag}_adj{k}.json" for s in us for k in ("false", "true")]
    return out + [f"fx_rates/USDKRW_{tag}.json", f"news_massive/ALL_{tag}.json"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, obj):
    """다 쓴 뒤에 바꿔 끼웁니다. 읽는 쪽(센서, 화면 내보내기)이 반쯤 쓴 파일을 보지 않게."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def window(path=None):
    from ai.screen_data import WINDOW_FILE, read_window
    return read_window(path or WINDOW_FILE)


# ---------- Mac: 받기 ----------

def latest_kr(env, since, today):
    """국내 시세가 나온 마지막 거래일. 첫 국내 종목 하나를 since ~ today로 한 번 물어봅니다(호출 1~2번)."""
    sys.path.insert(0, str(ROOT / "loaders"))
    from urllib.parse import quote

    import daily_prices as dp
    from common import Http, need
    key = need(env, "DATA_GO_KR_SERVICE_KEY")
    http = Http(secrets=[key], min_interval=0.5)
    if "%" not in key:
        key = quote(key, safe="")
        http.secrets.append(key)
    code = symbols()[0][0]
    items = dp.fetch_kr(http, key, code, since, today)
    return max((datetime.strptime(i["basDt"], "%Y%m%d").date() for i in items if i.get("srtnCd") == code), default=None)


def pick_end(prev_end, latest, want=None):
    """(끝날, 까닭). 끝날이 None이면 받을 것이 없거나 받으면 안 됩니다."""
    if latest is None:
        return None, "국내 시세를 찾지 못했습니다(키나 API 상태를 보세요)"
    if want and want > latest:
        return None, f"국내 시세가 {latest}까지만 나왔습니다. --end를 그날 이하로 주거나 나온 뒤 다시 하세요"
    end = want or latest
    if end <= prev_end:
        return None, f"새로 받을 날이 없습니다(구간 끝 {prev_end}, 국내 시세 최신 {latest})"
    return end, ""


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


def run_loader(step):
    print("==", " ".join(step[:4]), flush=True)
    env = {**os.environ, "LOADER_RAW_DIR": RAW_WINDOW_REL}
    return subprocess.run([PY, *step], cwd=ROOT, env=env).returncode


def kr_last_days(tag, raw=RAW_WINDOW):
    kr, _ = symbols()
    out = {}
    for s in kr:
        rows = json.loads((raw / "daily_prices" / f"kr_{s}_{tag}.json").read_text(encoding="utf-8"))
        out[s] = max((datetime.strptime(r["basDt"], "%Y%m%d").date() for r in rows), default=None)
    return out


def fetch(args):
    sys.path.insert(0, str(ROOT / "loaders"))
    from common import load_env
    parts = window()
    prev_end = date.fromisoformat(parts[-1][1])
    today = datetime.now(SEOUL).date()
    end, why = pick_end(prev_end, latest_kr(load_env(), prev_end, today), args.end)
    if end is None:
        print(why)
        return 0 if why.startswith("새로 받을 날이 없습니다") else 1
    start = prev_end.isoformat()  # 하루 겹침
    tag = tag_of(start, end.isoformat())
    names = files(tag)
    fetch_steps, _ = steps(start, end.isoformat())
    print(f"새 구간 {start} ~ {end}(앞 구간의 끝날부터 하루 겹침), 원본 {len(names)}개 → {RAW_WINDOW_REL}/")
    if args.plan:
        for s in fetch_steps:
            print("  " + " ".join(["python", *s]))
        return 0
    if NEXT.exists():
        old = json.loads(NEXT.read_text(encoding="utf-8"))
        if (old["start"], old["end"]) == (start, end.isoformat()) and all(
                (RAW_WINDOW / n).exists() and sha(RAW_WINDOW / n) == old["files"].get(n) for n in names):
            print("같은 구간을 이미 받아 두었습니다(next.json의 지문이 같음)")
            return 0
    for s in fetch_steps:
        if run_loader(s) != 0:
            print("받기가 실패했습니다. next.json을 바꾸지 않았습니다")
            return 1
    missing = [n for n in names if not (RAW_WINDOW / n).exists()]
    if missing:
        print(f"받은 파일이 모자랍니다({len(missing)}개, 예: {missing[0]}). next.json을 바꾸지 않았습니다")
        return 1
    short = {s: d for s, d in kr_last_days(tag).items() if d != end}
    if short:
        print(f"국내 시세가 {end}까지 오지 않았습니다({', '.join(short)}). next.json을 바꾸지 않았습니다")
        return 1
    write_json(NEXT, {"why": "Mac이 받은 새 구간 원본. Airflow window_extend가 지문을 대조하고 원천에 넣은 뒤 구간 목록에 더함",
                      "start": start, "end": end.isoformat(), "got": datetime.now(SEOUL).isoformat(timespec="seconds"),
                      "files": {n: sha(RAW_WINDOW / n) for n in names}})
    print(f"{NEXT.relative_to(ROOT)}를 적었습니다. 다음: Airflow window_extend가 처리(또는 make window-load)")
    return 0


# ---------- Airflow: 처리 ----------

def pending(parts, nxt):
    """(종료 코드, 까닭, (시작, 끝)). 0 = 처리할 새 원본, NOT_YET = 아직 없음, 2 = 앞뒤가 안 맞음."""
    if nxt is None:
        return NOT_YET, "받아 둔 새 원본이 없습니다(호스트의 make window-fetch를 기다림)", None
    w_end = parts[-1][1]
    s, e = nxt["start"], nxt["end"]
    if e <= w_end:
        return NOT_YET, f"받아 둔 원본({s} ~ {e})은 이미 구간에 있습니다(호스트의 다음 make window-fetch를 기다림)", None
    if s > w_end:
        return 2, f"받아 둔 원본이 {s}부터라 구간 끝 {w_end}와 겹치지 않습니다(빈 날). 호스트에서 make window-fetch로 다시 받으세요", None
    return 0, f"처리할 새 원본: {s} ~ {e}(구간 끝 {w_end})", (s, e)


def read_next(path=NEXT):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def check_files(nxt, raw=RAW_WINDOW, names=None):
    """next.json의 파일 목록이 그 구간의 원본 38개와 같고, 지문이 모두 같은지. 틀린 것의 목록을 돌려줍니다."""
    names = names if names is not None else files(tag_of(nxt["start"], nxt["end"]))
    bad = sorted(set(names) ^ set(nxt["files"]))
    for n in names:
        if n in nxt["files"] and (not (raw / n).exists() or sha(raw / n) != nxt["files"][n]):
            bad.append(n)
    return bad


def check(args):
    parts = window()
    nxt = read_next()
    code, why, part = pending(parts, nxt)
    print(why)
    if code:
        return code
    start, end = part
    tag = tag_of(start, end)
    bad = check_files(nxt)
    if bad:
        print(f"원본 지문이 next.json과 다릅니다({len(bad)}개, 예: {bad[0]}). 받는 중이었거나 바뀜. 호스트에서 make window-fetch로 다시 받으세요")
        return 2
    short = {s: d for s, d in kr_last_days(tag).items() if str(d) != end}
    if short:
        print(f"국내 시세가 {end}까지 없습니다({', '.join(short)})")
        return 2
    from ai.screen_data import TAGS, instruments, read_raw
    stat = {}
    read_raw(instruments(), TAGS + [tag], overlap=stat)
    if stat["diff"]:
        label, key = stat["diff"][0]
        print(f"앞 구간과 겹치는 날의 값이 다릅니다({len(stat['diff'])}건, 예: {label} {key[0]} {key[1]})")
        return 2
    print(f"원본 {len(nxt['files'])}개 지문 같음, 국내 끝날 {end}, 겹치는 날 값 {stat['same']}개 같음")
    print(f"{start} {end}")
    return 0


def snapshot(con, start, end):
    prices = {(s, d): v for s, d, *v in con.execute(
        "select symbol, trade_date, open_price, high_price, low_price, close_price, volume from source.daily_prices "
        "where trade_date between %s and %s", (start, end)).fetchall()}
    fx = {d: r for d, r in con.execute(
        "select rate_date, rate from source.fx_rates where currency_pair = 'USDKRW' and rate_date between %s and %s",
        (start, end)).fetchall()}
    return prices, fx


def changed(before, after):
    """새로 생겼거나 값이 바뀐 키의 수. 적재기는 값이 같은 행을 고치지 않으므로(CDC 이벤트 없음) 이 수만큼 Bronze에 닿아야 합니다."""
    return sum(1 for k, v in after.items() if before.get(k) != v)


def load(args):
    sys.path.insert(0, str(ROOT / "loaders"))
    from common import connect, load_env
    start, end = args.start, args.end
    env = load_env()
    with connect(env) as con:
        before = snapshot(con, start, end)
        lsn_from = int(con.execute("select (pg_current_wal_lsn() - '0/0'::pg_lsn)::bigint").fetchone()[0])
    _, load_steps = steps(start, end)
    for s in load_steps:
        if run_loader(s) != 0:
            print("원천 적재가 실패했습니다. 구간은 더하지 않았습니다(다시 돌려도 값이 같은 행은 그대로)")
            return 1
    with connect(env) as con:
        after = snapshot(con, start, end)
    n_prices, n_fx = changed(before[0], after[0]), changed(before[1], after[1])
    print(f"원천에서 바뀐 행: 시세 {n_prices}, 환율 {n_fx}(값이 같은 행은 고치지 않음)")
    print(json.dumps({"lsn_from": lsn_from, "daily_prices": n_prices, "fx_rates": n_fx}))
    return 0


BRONZE_KEY = {"daily_prices": "concat_ws(':', get_json_object(r, '$.symbol'), get_json_object(r, '$.trade_date'))",
              "fx_rates": "concat_ws(':', get_json_object(r, '$.currency_pair'), get_json_object(r, '$.rate_date'))"}


def bronze_sql(table, lsn_from):
    return (f"select count(distinct k) from (select {BRONZE_KEY[table]} as k from ("
            f"select get_json_object(payload, '$.after') as r, "
            f"cast(get_json_object(payload, '$.source.lsn') as bigint) as lsn from lake.bronze.{table}) "
            f"where lsn >= {int(lsn_from)} and r is not null)")


def wait_bronze(args):
    try:
        ch = json.loads(args.changes)
        want = {t: int(ch[t]) for t in BRONZE_KEY}
        lsn_from = int(ch["lsn_from"])
    except (TypeError, ValueError, KeyError):
        print(f"앞 태스크(load_source)의 결과를 받지 못했습니다: {str(args.changes)[:80]!r}")
        return 2
    if not any(want.values()):
        print("원천에서 바뀐 행이 없어 기다릴 것이 없습니다")
        return 0
    from ai.screen_data import _spark_rows
    short = []
    for t, n in want.items():
        if not n:
            continue
        got = int(_spark_rows(bronze_sql(t, lsn_from))[0][0])
        print(f"  Bronze {t}: {got} / {n}")
        if got < n:
            short.append(t)
    if short:
        print(f"아직 Bronze에 닿지 않음: {', '.join(short)}(Flink checkpoint는 10초마다)")
        return NOT_YET
    print("바뀐 행이 모두 Bronze에 닿음")
    return 0


def add_part(path, start, end, got, note):
    """구간 목록에 (start, end)를 더합니다. 이미 그 끝날까지 있으면 그대로(다시 돌려도 같음). 0 = 더함, 1 = 그대로."""
    from ai.screen_data import BASE, read_window
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
    else:
        doc = {"why": "화면 데이터 기간을 이루는 원본 구간(ai/screen_data.py read_window). 첫 구간은 평가 기준 2년치라 바꾸지 않고, "
                      "늘린 구간은 Airflow window_extend가 뒤에 더합니다(docs/decisions/0015, 0016). git 밖 파일입니다.",
               "parts": [{"start": BASE[0], "end": BASE[1], "got": "2026-11-13", "note": "평가 기준 2년치(원본 지문은 eval/f_screen/lock.json)"}]}
    last = doc["parts"][-1]["end"]
    if end <= last:
        print(f"구간 목록이 이미 {last}까지 있어 그대로 둡니다")
        return 1
    doc["parts"].append({"start": start, "end": end, "got": got, "note": note})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    try:
        read_window(Path(tmp))  # 이어짐 검사(빈 날, 끝이 늘지 않음)에 걸리면 지금 목록은 그대로
    except ValueError:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)
    print(f"구간 목록에 {start} ~ {end}를 더했습니다(이제 {len(doc['parts'])}구간)")
    return 0


def add(args):
    from ai.screen_data import WINDOW_FILE
    nxt = read_next() or {}
    got = (nxt.get("got") or "")[:10] if (nxt.get("start"), nxt.get("end")) == (args.start, args.end) else ""
    add_part(WINDOW_FILE, args.start, args.end, got, "Mac fetch, Airflow window_extend")
    return 0


def confirm(args):
    from ai.screen_data import OUT, status
    st = status(OUT)
    end = (st.get("window") or [None, None])[1]
    if end != args.end or not st.get("ok"):
        names = [f["name"] for f in st.get("failures") or []]
        print(f"화면 파일 기간 끝 {end}(기대 {args.end}), 검사 {'통과' if st.get('ok') else '실패 ' + ', '.join(names)}")
        return 1
    print(f"화면 파일이 {end}까지이고 검사가 모두 통과했습니다")
    return 0


# ---------- 자체 시험 ----------

def selftest():
    results = []

    def ok(name, cond, got=""):
        results.append((name, bool(cond), got))

    thu, fri, sat, sun, mon = (date(2026, 11, d) for d in (19, 20, 21, 22, 23))
    # 끝날 고르기: 국내 시세가 나온 마지막 거래일. 일요일에 돌려도 금요일 시세가 아직 없으면 목요일까지만
    ok("일요일, 금요일 국내 시세 나옴 → 금요일까지", pick_end(thu, fri)[0] == fri, pick_end(thu, fri))
    ok("월요일 오전, 금요일 국내 시세 아직 → 받을 것 없음", pick_end(thu, thu)[0] is None, pick_end(thu, thu))
    ok("--end가 국내 시세보다 늦음 → 거절", pick_end(thu, fri, mon)[0] is None and "까지만" in pick_end(thu, fri, mon)[1])
    ok("--end로 앞당김", pick_end(date(2026, 11, 18), fri, thu)[0] == thu)
    ok("국내 시세를 못 찾음 → 받지 않음", pick_end(thu, None)[0] is None)
    ok("휴장 뒤(12/31 휴장, 1/2 시세) → 1/2까지",
       pick_end(date(2026, 12, 30), date(2027, 1, 2))[0] == date(2027, 1, 2))
    # 처리할 것 고르기
    base = [("2024-11-13", "2026-11-12"), ("2026-11-12", "2026-11-18")]
    nx = {"start": "2026-11-18", "end": "2026-11-19"}
    ok("next.json 없음 → 아직", pending(base, None)[0] == NOT_YET)
    ok("이미 구간에 있음 → 아직", pending(base, {"start": "2026-11-12", "end": "2026-11-18"})[0] == NOT_YET)
    ok("하루 겹침 → 처리", pending(base, nx)[0:3:2] == (0, ("2026-11-18", "2026-11-19")))
    ok("여러 날 겹침(앞 구간 안에서 시작) → 처리", pending(base, {"start": "2026-11-16", "end": "2026-11-20"})[0] == 0)
    ok("빈 날(앞 구간 끝 다음 날보다 뒤) → 오류", pending(base, {"start": "2026-11-20", "end": "2026-11-23"})[0] == 2)
    # 바뀐 행 세기
    b = {("A", 1): (1, 2), ("A", 2): (3, 4)}
    a = {("A", 1): (1, 2), ("A", 2): (3, 5), ("A", 3): (6, 7)}
    ok("바뀐 행 = 고친 1 + 새로 1", changed(b, a) == 2, changed(b, a))
    ok("같은 원본으로 다시 넣음 → 0", changed(a, dict(a)) == 0)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        names = files("20261118_20261119", ["005930"], ["SPY"])
        for n in names:
            (td / n).parent.mkdir(parents=True, exist_ok=True)
            (td / n).write_text("[]", encoding="utf-8")
        nxt = {"start": "2026-11-18", "end": "2026-11-19", "files": {n: sha(td / n) for n in names}}
        ok("원본 지문 같음", check_files(nxt, td, names) == [], check_files(nxt, td, names))
        (td / names[1]).write_text("[1]", encoding="utf-8")
        ok("받은 뒤 파일 하나 바뀜 → 걸림", check_files(nxt, td, names) == [names[1]])
        short = dict(nxt, files={k: v for k, v in nxt["files"].items() if k != names[0]})
        ok("next.json에 파일 하나 빠짐 → 걸림", names[0] in check_files(short, td, names))
        # 구간 더하기: 처음이면 첫 구간과 함께 만들고, 다시 돌리면 그대로, 빈 날이면 거절하고 목록은 그대로
        wf = td / "window.json"
        ok("목록 없음 → 첫 구간과 함께 만듦", add_part(wf, "2026-11-12", "2026-11-18", "2026-11-19", "t") == 0
           and len(json.loads(wf.read_text())["parts"]) == 2)
        ok("같은 구간 다시 → 그대로", add_part(wf, "2026-11-12", "2026-11-18", "2026-11-19", "t") == 1)
        before = wf.read_text()
        try:
            add_part(wf, "2026-11-20", "2026-11-23", "", "t")
            ok("빈 날 → 거절", False)
        except ValueError:
            ok("빈 날 → 거절, 목록 그대로", wf.read_text() == before and not list(td.glob("window.json*.tmp")))
        ok("하루 겹쳐 더함", add_part(wf, "2026-11-18", "2026-11-19", "", "t") == 0
           and window(wf)[-1] == ("2026-11-18", "2026-11-19"))
    ok("Bronze 질의에 lsn 기준과 after만", "lsn >= 42" in bronze_sql("daily_prices", 42) and "'$.after'" in bronze_sql("fx_rates", 1))
    for name, good, got in results:
        print(f"{'통과' if good else '실패'}  {name}" + ("" if good else f": {got}"))
    good = all(g for _, g, _ in results)
    print(f"자체 시험: {'통과' if good else '실패'} {sum(g for _, g, _ in results)}/{len(results)}")
    return 0 if good else 1


def main():
    sys.path.insert(0, str(ROOT))
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="VM, 키, 원본 없이 시험")
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch", help="Mac: 새 구간 원본을 받고 next.json을 적음")
    f.add_argument("--end", type=date.fromisoformat, help="새 끝날(그날 포함). 기본은 국내 시세가 나온 마지막 거래일")
    f.add_argument("--plan", action="store_true", help="할 일만 보이고 받지 않음")
    sub.add_parser("pending", help="받아 둔 새 원본이 있으면 0, 아직이면 99")
    sub.add_parser("check", help="next.json 지문, 국내 끝날, 겹치는 날 값 대조")
    for name in ("load", "add"):
        p = sub.add_parser(name)
        p.add_argument("start")
        p.add_argument("end")
    w = sub.add_parser("wait-bronze")
    w.add_argument("changes", help="load가 마지막 줄에 찍은 JSON")
    c = sub.add_parser("confirm")
    c.add_argument("end")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.cmd == "pending":
        code, why, _ = pending(window(), read_next())
        print(why)
        return code
    cmds = {"fetch": fetch, "check": check, "load": load, "wait-bronze": wait_bronze, "add": add, "confirm": confirm}
    if args.cmd not in cmds:
        ap.print_help()
        return 2
    return cmds[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
