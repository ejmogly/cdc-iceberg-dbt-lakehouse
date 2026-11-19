#!/usr/bin/env python3
"""F 화면 계산 서버를 평가 기준(docs/ai/F-screen.md 4절)으로 대조합니다. 서버를 같은 프로세스에 띄워 HTTP로 부릅니다.

사용: .venv/bin/python eval/f_screen/check_api.py            다섯 모두
      .venv/bin/python eval/f_screen/check_api.py golden     손익 정확도(골든) 30개
      .venv/bin/python eval/f_screen/check_api.py real       손익 정확도(실제 데이터) 12개, 입력 경고 8개, 새로 짠 계산
      .venv/bin/python eval/f_screen/check_api.py latency    계산 지연(매매 20줄 사례 200번, p95)
      .venv/bin/python eval/f_screen/check_api.py pipeline   파이프라인 상태(깬 원천, 깬 파일)
      .venv/bin/python eval/f_screen/check_api.py edges      입력 경계(잘못된 날짜와 수, 빠진 줄만 있는 매매, Host 머리)
      .venv/bin/python eval/f_screen/check_api.py accounts   증권사별 계좌와 지금 잔고(11/19 중간 판 의견, ai/holdings.py)
      .venv/bin/python eval/f_screen/check_api.py portfolio  전체 탭(POST /api/portfolio): 종목별 손익 API의 합, 평가액, 비중, 증권사별 합

- 골든: E01 골든 30개(eval/e01_pnl/cases.json)를 골든용 파일(data/generated/screen_golden.duckdb)로 띄운 서버에 넣고
  정답과 맞춥니다. 허용 오차는 E01과 같음(원은 원 단위, 달러는 센트, 연 환산 0.05%p). 경고는 여덟 코드를 한 건씩
  비교합니다. 기대 경고는 E01 정답(expected.json)의 손익 SQL 경고 다섯에, 화면 경고 FX_CARRIED를 4절 규칙대로
  E01의 가짜 시세(fixture.json)에서 따로 세어 더한 것입니다(E01 정답에는 화면 경고가 없음).
- 실제 데이터: 사례 20개(eval/f_screen/cases_real.json)를 화면 데이터 파일로 띄운 서버에 넣고 독립 정답
  (data/generated/f_screen/expected_real.json)과 맞춥니다. 체결가 규칙(p_rule)과 환율 배수(fx_factor)는 정답 파일의
  inputs에 남은 입력 값을 그대로 넣습니다(입력이지 정답이 아님). 경고는 여덟 코드와 문구에 채운 날짜(원본에서 따로
  셈), 큰 움직임, SPY 선의 끝 값, 일별 선의 끝 값, 선으로 다시 센 최대 낙폭까지 봅니다.
- 결과 파일은 data/generated/f_screen/api_check.json(git 제외, 어긋난 칸에 실제 값이 들어갈 수 있음)입니다.
"""
import json
import math
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import duckdb  # noqa: E402
import real_world  # noqa: E402  독립 정답과 같은 원본 읽기(정답 쪽 코드). 문구의 날짜를 따로 셀 때만 씀

from ai import llm, screen_data, screen_server  # noqa: E402
from ai.screen_server import SAFE, serve, stop  # noqa: E402

E01 = ROOT / "eval" / "e01_pnl"
HERE = Path(__file__).parent
GEN = ROOT / "data" / "generated" / "f_screen"
EXACT = ["qty_left", "avg_cost_local", "avg_buy_fx", "realized_local", "unrealized_local", "total_local",
         "realized_krw", "unrealized_krw", "total_krw", "invested_krw", "simple_return_pct", "price_effect_krw",
         "fx_effect_krw", "cross_effect_krw", "bench_total_krw", "max_drawdown_krw", "naive_krw"]
NEAR = ["irr_pct", "bench_irr_pct"]
DROPPED_BY_SQL = {"OVERSOLD", "AFTER_ASOF", "BAD_QTY"}


def http(base, path, body=None, host=None, headers=None):
    headers = {"Content-Type": "application/json", **(headers or {})}
    if host:
        headers["Host"] = host
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return Decimal(str(a)) == Decimal(str(b))


def api_trades(trades, inputs=None):
    out = []
    for k, t in enumerate(trades):
        u = (inputs or {}).get(str(k), t)
        row = {"date": u["d"], "side": u["side"], "qty": u["q"] if str(k) in (inputs or {}) else t["q"]}
        if "p" in u:
            row["price"] = u["p"]
        if "fx" in u:
            row["fx_rate"] = u["fx"]
        out.append(row)
    return out


def compare(exp, got, warnings=None):
    """정답과 API 결과의 어긋난 칸 목록. warnings를 주면 정답 파일의 경고 대신 그것과 비교합니다."""
    s = got.get("summary") or {}
    bad = [f"{k}: 정답 {exp[k]}, API {s.get(k)}" for k in EXACT if k in exp and not same(exp[k], s.get(k))]
    for k in NEAR:
        e, g = exp.get(k), s.get(k)
        if (e is None) != (g is None) or (e is not None and abs(Decimal(str(e)) - Decimal(str(g))) > Decimal("0.05")):
            bad.append(f"{k}: 정답 {e}, API {g}")
    want = [(x["d"], x["q"], Decimal(str(x["price"])), Decimal(str(x["avg_cost"])), Decimal(str(x["pnl_krw"])))
            for x in exp["sells"]]
    have = [(x["date"], x["qty"], Decimal(str(x["price"])), Decimal(str(x["avg_cost_before"])),
             Decimal(str(x["realized_krw"]))) for x in got["sells"]]
    if want != have:
        bad.append(f"sells: 정답 {len(want)}건, API {len(have)}건 또는 값 다름")
    ew = sorted((w["code"], w["trade"]) for w in (exp["warnings"] if warnings is None else warnings))
    gw = sorted((w["code"], w["trade_index"]) for w in got["warnings"])
    if ew != gw:
        bad.append(f"warnings: 정답 {ew}, API {gw}")
    return bad


def new_calcs(exp, got):
    """새로 짠 계산: 일별 선의 끝 값 = total_krw, SPY 선의 끝 값 = bench_total_krw, 선으로 다시 센 낙폭 = 낙폭.
    장부에 들어간 매수가 있는데 일별 선이 비면 실패입니다."""
    d = got["daily"]
    if not got.get("booked"):
        return [] if not d else ["장부에 든 매수가 없는데 일별 선이 있음"]
    if not d:
        return ["장부에 든 매수가 있는데 일별 선이 비었음"]
    bad = []
    if d[-1]["date"] != got["asof"]:
        bad.append(f"daily 마지막 날 {d[-1]['date']} ≠ 기준일 {got['asof']}")
    if not same(d[-1]["pnl_krw"], exp["total_krw"]):
        bad.append(f"일별 선 끝 {d[-1]['pnl_krw']} ≠ total_krw {exp['total_krw']}")
    if not same(d[-1]["bench_krw"], exp["bench_total_krw"]):
        bad.append(f"SPY 선 끝 {d[-1]['bench_krw']} ≠ bench_total_krw {exp['bench_total_krw']}")
    low, peak = 0, 0
    for x in d:
        peak = max(peak, x["pnl_krw"])
        low = min(low, x["pnl_krw"] - peak)
    if abs(low - exp["max_drawdown_krw"]) > 1:
        bad.append(f"선으로 다시 센 낙폭 {low} ≠ max_drawdown_krw {exp['max_drawdown_krw']}")
    return bad


def golden_fx_carried(fix, case, exp):
    """4절 「입력 경고」의 FX_CARRIED를 E01 가짜 시세에서 따로 셉니다: 미국 종목, 환율을 넣지 않음, 데이터 기간 안,
    정수 수량, 손익 SQL이 빼지 않은 줄(E01 정답의 초과 매도, 기준일 뒤, 수량 0 이하 경고가 없는 줄), 달력으로 맞춘
    그날의 환율이 없음."""
    sym = fix["symbols"][case["symbol"]]
    if sym["currency"] != "USD":
        return []
    first, last = min(sym["rows"]), max(sym["rows"])
    dropped = {w["trade"] for w in exp["warnings"] if w["code"] in DROPPED_BY_SQL}
    out = []
    for k, t in enumerate(case["trades"]):
        if "fx" in t or k in dropped or not first <= t["d"] <= last or float(t["q"]) != int(t["q"]):
            continue
        d = max(c for c in fix["calendar"] if c <= t["d"])
        if d not in fix["usdkrw"]:
            out.append({"code": "FX_CARRIED", "trade": k})
    return out


def check_golden():
    db = screen_data.GOLDEN_OUT
    if not db.exists():
        screen_data.golden(db)
    httpd, _ = serve(db, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    fix = json.loads((E01 / "fixture.json").read_text(encoding="utf-8"))
    cases = json.loads((E01 / "cases.json").read_text(encoding="utf-8"))
    expected = json.loads((E01 / "expected.json").read_text(encoding="utf-8"))
    rows, added = [], []
    for c in cases:
        exp = expected[c["id"]]
        fxc = golden_fx_carried(fix, c, exp)
        added += [(c["id"], w["trade"]) for w in fxc]
        code, got = http(base, "/api/pnl", {"symbol": c["symbol"], "asof": c["asof"], "trades": api_trades(c["trades"])})
        if code != 200:
            rows.append({"id": c["id"], "pass": False, "bad": [f"HTTP {code} {got}"]})
            continue
        bad = compare(exp, got, warnings=exp["warnings"] + fxc) + new_calcs(exp, got)
        rows.append({"id": c["id"], "pass": not bad, "bad": bad})
    stop(httpd)
    n = sum(r["pass"] for r in rows)
    print(f"골든 {n}/{len(rows)}(E01 정답 칸, 매도, 경고 여덟 코드 한 건씩, 일별 선과 SPY 선의 끝 값, 선으로 센 낙폭). "
          f"E01 정답에 더한 FX_CARRIED {len(added)}줄({', '.join(f'{i}:{k}' for i, k in added) or '없음'})")
    for r in rows:
        if not r["pass"]:
            print(f"  실패 {r['id']}: " + "; ".join(r["bad"]))
    return {"pass": n, "cases": len(rows), "rows": rows, "fx_carried_added": added}


def fill_expected(world, symbol, code, d):
    """문구에 채울 날짜를 원본에서 따로 셉니다(real_world.load: 독립 정답과 같은 원본 읽기)."""
    rows = sorted(world["symbols"][symbol]["rows"])
    if code == "NON_TRADING_DAY":
        return {"used_date": max(x for x in rows if x <= d)}
    if code == "OUT_OF_DATA_RANGE":
        return {"first": rows[0], "last": rows[-1]}
    if code == "FX_CARRIED":
        snapped = max(x for x in world["calendar"] if x <= d)
        return {"fx_date": max(x for x in world["usdkrw"] if x <= snapped)}
    return {}


def check_real():
    db = screen_data.OUT
    httpd, _ = serve(db, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    cases = json.loads((HERE / "cases_real.json").read_text(encoding="utf-8"))
    expected = json.loads((GEN / "expected_real.json").read_text(encoding="utf-8"))
    world = real_world.load()
    texts = json.loads((HERE / "warnings.json").read_text(encoding="utf-8"))
    rows, filled = [], 0
    for c in cases:
        exp = expected[c["id"]]
        trades = api_trades(c["trades"], exp["inputs"])
        code, got = http(base, "/api/pnl", {"symbol": c["symbol"], "asof": c["asof"], "trades": trades})
        if code != 200:
            rows.append({"id": c["id"], "group": c["group"], "pass": False, "bad": [f"HTTP {code} {got}"]})
            continue
        bad = compare(exp, got) + new_calcs(exp, got)
        if "big_moves" in exp:
            b = got["big_moves"] or {}
            for k in ("dates", "judged_from", "not_judged_in_holding"):
                if b.get(k) != exp["big_moves"][k]:
                    bad.append(f"big_moves.{k}: 정답 {len(exp['big_moves'][k]) if k == 'dates' else exp['big_moves'][k]}"
                               f", API {len(b.get(k) or []) if k == 'dates' else b.get(k)}")
        if c.get("expect_warnings"):
            want = sorted((w["code"], w["trade"]) for w in c["expect_warnings"])
            have = sorted((w["code"], w["trade_index"]) for w in got["warnings"] if (w["code"], w["trade_index"]) in want)
            if want != have:
                bad.append(f"사례가 정한 경고 {want} 없음")
        for w in got["warnings"]:
            fill = fill_expected(world, c["symbol"], w["code"], trades[w["trade_index"]]["date"])
            text = texts[w["code"]].format(**fill)
            if w["message"] != text or any(w.get(k) != v for k, v in fill.items()):
                bad.append(f"문구 다름: {w['code']} {w['trade_index']}번 줄")
            filled += bool(fill)
        rows.append({"id": c["id"], "group": c["group"], "pass": not bad, "bad": bad})
    stop(httpd)
    real = [r for r in rows if r["id"].startswith("R")]
    warn = [r for r in rows if r["id"].startswith("W")]
    print(f"실제 데이터 {sum(r['pass'] for r in real)}/{len(real)}(정답 칸, 매도, 경고, 큰 움직임, 일별 선과 SPY 선의 끝 값, 낙폭), "
          f"입력 경고 {sum(r['pass'] for r in warn)}/{len(warn)}(문구에 날짜를 채운 경고 {filled}건을 원본에서 따로 셈)")
    for r in rows:
        if not r["pass"]:
            print(f"  실패 {r['id']}: " + "; ".join(r["bad"]))
    return {"real_pass": sum(r["pass"] for r in real), "real": len(real), "warn_pass": sum(r["pass"] for r in warn),
            "warn": len(warn), "filled_messages": filled, "rows": rows}


def latency_case():
    """매매 20줄: AAPL을 2024-12 ~ 2026-07 달마다 10일에 사고, 다섯째마다 15주를 팖(초과 매도 없음)."""
    trades, months = [], [(2024 + (11 + i) // 12, (11 + i) % 12 + 1) for i in range(20)]
    for i, (y, m) in enumerate(months):
        sell = i % 5 == 4
        trades.append({"date": f"{y}-{m:02d}-10", "side": "SELL" if sell else "BUY", "qty": 15 if sell else 10})
    return {"symbol": "AAPL", "asof": "2026-11-11", "trades": trades}


def check_latency(n=200):
    httpd, _ = serve(screen_data.OUT, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    body = latency_case()
    code, first = http(base, "/api/pnl", body)
    times, server = [], []
    for _ in range(n):
        t0 = time.perf_counter()
        code, got = http(base, "/api/pnl", body)
        times.append(time.perf_counter() - t0)
        server.append(got.get("seconds", 0))
        assert code == 200 and got["summary"] == first["summary"]
    stop(httpd)
    p95 = sorted(times)[math.ceil(0.95 * n) - 1]
    out = {"calls": n, "p50_ms": round(sorted(times)[n // 2] * 1000, 1), "p95_ms": round(p95 * 1000, 1),
           "max_ms": round(max(times) * 1000, 1), "server_p95_ms": round(sorted(server)[math.ceil(0.95 * n) - 1] * 1000, 1),
           "pass": p95 <= 0.3, "warnings_in_case": len(first["warnings"])}
    print(f"계산 지연: 매매 20줄 x {n}번, p50 {out['p50_ms']}ms, p95 {out['p95_ms']}ms(통과선 300ms), "
          f"최대 {out['max_ms']}ms, 서버 안 계산 p95 {out['server_p95_ms']}ms → {'통과' if out['pass'] else '실패'}")
    return out


def _gated(db, name, want_failure):
    """서버를 db로 띄워 상태가 실패하고, 손익 API가 숫자 없이 503과 그 실패 항목을 돌려주는지."""
    body = latency_case()
    httpd, _ = serve(db, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    _, st = http(base, "/api/status")
    p_code, p = http(base, "/api/pnl", body)
    stop(httpd)
    names = [f["name"] for f in p.get("failures", [])]
    ok = not st["ok"] and p_code == 503 and "summary" not in p and want_failure in names
    return {"break": name, "pass": ok, "status_ok": st["ok"], "pnl_http": p_code, "failures": names}


def check_pipeline():
    """깨진 원천으로 내보낸 파일(내보내기 검사가 실패)과, 내보낸 뒤 깬 파일(지문이 다름) 모두에서 서버가 숫자 대신
    503과 실패 항목을 돌려주는지. 원천은 VM 없이 원본으로 대신합니다(원본과 같으면 모든 검사 통과)."""
    breaks = {
        "행 하나 지우기": "delete from prices where symbol = 'SPY' and trade_date = (select min(trade_date) + 30 "
                     "from prices where symbol = 'SPY')",
        "환율 하루 빼기": "delete from fx where rate_date = (select max(rate_date) - 10 from fx)",
        "종가 하나 바꾸기": "update prices set close = close * 1.01 where symbol = 'NVDA' and trade_date = "
                       "(select max(trade_date) - 20 from prices where symbol = 'NVDA')",
    }
    insts = screen_data.instruments()
    raw_prices, raw_adj, raw_fx = screen_data.read_raw(insts)
    no_golden = lambda: (True, "건너뜀")
    keys = sorted(raw_prices)
    src_breaks = {
        "Gold 행 하나 빠짐": ({k: v for k, v in raw_prices.items() if k != keys[len(keys) // 2]}, raw_fx,
                       "Gold와 원본의 시세 행 수"),
        "Gold 환율 하루 빠짐": (raw_prices, {d: r for d, r in raw_fx.items() if d != sorted(raw_fx)[100]},
                         "환율 대조(Gold와 원본 하루씩)"),
        "Gold 종가 하나 다름": ({k: (v[:3] + ((v[3] * Decimal("1.01")).quantize(Decimal("0.0001")),) + v[4:])
                           if k == keys[len(keys) // 3] else v for k, v in raw_prices.items()}, raw_fx,
                          "시세 값 대조(Gold와 원본 한 행씩)"),
    }
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (name, (prices, fx, want)) in enumerate(src_breaks.items()):
            db = Path(tmp) / f"src_{i}.duckdb"
            checks = screen_data.check_real(insts, prices, fx, 0, raw_prices, raw_adj, raw_fx, golden=no_golden)
            screen_data.write_real(db, insts, prices, fx, raw_adj, [], checks)
            out.append(_gated(db, name, want))
        for i, (name, sql) in enumerate(breaks.items()):
            # 깨는 경우마다 다른 파일에 씁니다.
            db = Path(tmp) / f"file_{i}.duckdb"
            shutil.copy(screen_data.OUT, db)
            con = duckdb.connect(str(db))
            con.execute(sql)
            con.close()
            out.append(_gated(db, "내보낸 뒤 " + name, "내보낸 뒤 바뀌지 않음(지문)"))
    httpd, _ = serve(screen_data.OUT, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    _, st = http(base, "/api/status")
    p_code, _ = http(base, "/api/pnl", latency_case())
    stop(httpd)
    control = st["ok"] and p_code == 200
    reload_rows = check_reload()
    reload_ok = all(r["pass"] for r in reload_rows)
    print(f"파이프라인 상태: 깬 것 {sum(o['pass'] for o in out)}/{len(out)}을 잡고 손익 API가 503과 실패 항목을 돌려줌, "
          f"깨지 않은 파일은 {'통과와 200' if control else '실패'}, "
          f"켠 채 파일 바꾸기 {sum(r['pass'] for r in reload_rows)}/{len(reload_rows)}")
    for o in out:
        print(f"  {'통과' if o['pass'] else '실패'}  {o['break']}: 상태 {o['status_ok']}, 손익 API {o['pnl_http']}, "
              f"실패 항목 {o['failures']}")
    for r in reload_rows:
        print(f"  {'통과' if r['pass'] else '실패'}  켠 채 {r['step']}: 상태 {r['status_ok']}, 손익 API {r['pnl_http']}, "
              f"다시 읽은 횟수 {r['reloads']}, 실패 항목 {r['failures']}")
    return {"pass": sum(o["pass"] for o in out), "breaks": len(out), "control_ok": control and reload_ok, "rows": out,
            "reload": reload_rows}


def check_reload():
    """서버를 켠 채 화면 파일을 바꾸면(Airflow screen_export가 새 파일을 씀) 다음 요청에서 다시 읽는지.
    좋은 파일 → 내보낸 뒤 깬 파일 → 좋은 파일 → 읽을 수 없는 파일 순서로 바꾸고, 바꿀 때마다 상태와 손익 API를 봅니다."""
    body = latency_case()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        db, bad, good, junk = (Path(tmp) / n for n in ("screen.duckdb", "bad.duckdb", "good.duckdb", "junk.duckdb"))
        for p in (db, bad, good):
            shutil.copy(screen_data.OUT, p)
        con = duckdb.connect(str(bad))
        con.execute("update prices set close = close * 1.01 where symbol = 'SPY' and trade_date = "
                    "(select max(trade_date) - 20 from prices where symbol = 'SPY')")
        con.close()
        junk.write_bytes(b"not a duckdb file")
        httpd, _ = serve(db, 0)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            for step, src, want_ok, want_reloads, want_failure in (
                    ("처음", None, True, 0, None),
                    ("깬 파일로 바꿈", bad, False, 1, "내보낸 뒤 바뀌지 않음(지문)"),
                    ("좋은 파일로 되돌림", good, True, 2, None),
                    ("읽을 수 없는 파일로 바꿈", junk, False, 3, "화면 파일을 다시 읽지 못함")):
                if src is not None:
                    os.replace(src, db)
                _, st = http(base, "/api/status")
                code, p = http(base, "/api/pnl", body)
                names = [f["name"] for f in st.get("failures", [])]
                ok = (st["ok"] == want_ok and st.get("reloads") == want_reloads
                      and code == (200 if want_ok else 503) and ("summary" in p) == want_ok
                      and (want_failure is None or want_failure in names))
                rows.append({"step": step, "pass": ok, "status_ok": st["ok"], "pnl_http": code,
                             "reloads": st.get("reloads"), "failures": names})
        finally:
            stop(httpd)
    return rows


def check_edges():
    """입력 경계. 잘못된 입력은 연결을 끊지 않고 400과 맞는 코드, 빠진 줄만 있으면 보유 기간과 큰 움직임이 없음."""
    httpd, _ = serve(screen_data.OUT, 0)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    buy = lambda d, q=1, **kw: {"date": d, "side": "BUY", "qty": q, **kw}
    pnl = lambda trades, asof="2026-11-11", sym="SPY": http(base, "/api/pnl", {"symbol": sym, "asof": asof,
                                                                                "trades": trades})
    tests = []

    def expect(name, result, code, error=None):
        c, r = result
        tests.append((name, c == code and (error is None or r.get("error") == error), f"{c} {r.get('error', '')}"))

    expect("날짜 20250304", pnl([buy("20250304")]), 400, "BAD_DATE")
    expect("날짜 2025-W10-2", pnl([buy("2025-W10-2")]), 400, "BAD_DATE")
    expect("기준일 20261111", pnl([buy("2025-03-04")], asof="20261111"), 400, "BAD_DATE")
    expect("수량 1e10", pnl([buy("2025-03-04", 1e10)]), 400, "BAD_TRADE")
    expect("수량 Infinity", pnl([buy("2025-03-04", "Infinity")]), 400, "BAD_TRADE")
    expect("체결가 소수 다섯째 자리", pnl([buy("2025-03-04", 1, price="1.23456")]), 400, "BAD_TRADE")
    expect("환율 0", pnl([buy("2025-03-04", 1, fx_rate=0)]), 400, "BAD_TRADE")
    expect("종목 목록 모양", pnl([buy("2025-03-04")], sym=["SPY"]), 400, "UNKNOWN_SYMBOL")
    c, r = http(base, "/api/status", host=f"evil.example:{port}")
    tests.append(("Host 머리가 다름", c == 403, str(c)))

    c, r = pnl([buy("2025-03-03", 0)])
    tests.append(("수량 0 한 줄만", c == 200 and not r["booked"] and r["holding"] is None and r["big_moves"] is None
                  and r["daily"] == [], f"{c} booked={r.get('booked')} holding={r.get('holding')}"))
    c, r = pnl([{"date": "2024-11-14", "side": "SELL", "qty": 5}, buy("2025-09-02", 5)])
    tests.append(("첫 줄이 초과 매도", c == 200 and r["holding"] == ["2025-09-02", "2026-11-11"]
                  and all(d >= "2025-09-02" for d in r["big_moves"]["dates"]), f"{c} holding={r.get('holding')}"))
    c, r = pnl([buy("2026-03-03"), buy("2026-04-01")], asof="2026-01-10")
    tests.append(("모든 줄이 기준일 뒤", c == 200 and r["holding"] is None and r["big_moves"] is None,
                  f"{c} holding={r.get('holding')}"))
    c, r = pnl([buy("2024-06-03"), buy("2025-07-01", 2.5)])  # 기간 밖 한 줄, 기간 안의 소수 수량 한 줄
    tests.append(("모든 줄이 앞에서 빠짐", c == 200 and r["summary"] is None and not r["booked"]
                  and sorted(w["code"] for w in r["warnings"]) == ["FRACTIONAL_QTY", "OUT_OF_DATA_RANGE"],
                  f"{c} summary={r.get('summary')}"))
    # 손익 SQL의 decimal 범위를 넘는 금액(미국 종목에 원화 체결가를 넣은 실수): 500이 아니라 400과 금액이 가장 큰 줄
    c, r = pnl([buy("2025-03-04", 1), buy("2025-04-01", 10, price="7000000")])
    tests.append(("금액이 계산 범위를 넘음", c == 400 and r.get("error") == "AMOUNT_TOO_LARGE" and r.get("trade_index") == 1,
                  f"{c} {r.get('error')} trade_index={r.get('trade_index')}"))
    c, r = pnl([buy("2026-10-20")])
    c2, r2 = pnl([buy("2025-03-04")])
    tests.append(("연 환산이 없는 까닭(30일 미만)", c == 200 and r["summary"]["irr_pct"] is None and r["irr_short"] is True
                  and c2 == 200 and r2["irr_short"] is False, f"{c} irr_short={r.get('irr_short')}, 대조 {r2.get('irr_short')}"))

    # 9번 칸(묻고 답하기): 모델을 부르지 않는 가짜 답하는 함수로 입력 경계와, 넣은 매매와 기준일이 도구 층에 가는지 봄
    seen = {}

    def fake(ts, q):
        seen.update(today=ts.today, trades=ts.account["trades"], text=q["text"], today_q=q["today"])
        return ({"status": "answered", "text": "가짜 답", "numbers": [], "dates_used": [], "warnings": []},
                {"tool_calls": [{"name": "price"}], "llm": [{"cost_usd": 0.001}], "decided_by": None})

    def broke(ts, q):
        raise llm.BudgetExceeded("상한")

    httpd.app.ask_fn = fake
    ask = lambda body: http(base, "/api/ask", body)
    good = {"question": " SPY 종가? ", "asof": "2026-11-11",
            "account": {"SPY": [buy("2025-03-04", "10"), {"date": "2025-06-02", "side": "SELL", "qty": "4"}], "QQQ": []}}
    expect("묻기: 질문 없음", ask({**good, "question": "  "}), 400, "NO_QUESTION")
    expect("묻기: 질문 301자", ask({**good, "question": "가" * 301}), 400, "TOO_LONG")
    expect("묻기: 기준일 틀림", ask({**good, "asof": "2026-13-01"}), 400, "BAD_DATE")
    expect("묻기: 기준일 기간 밖", ask({**good, "asof": "2030-01-02"}), 400, "OUT_OF_DATA_RANGE")
    expect("묻기: 넣은 매매에 없는 종목", ask({**good, "account": {"NOPE": [buy("2025-03-04")]}}), 400, "UNKNOWN_SYMBOL")
    expect("묻기: 넣은 매매의 틀린 줄", ask({**good, "account": {"SPY": [buy("20250304")]}}), 400, "BAD_DATE")
    c, r = ask(good)
    got_trades = seen.get("trades", {})
    tests.append(("묻기: 기준일이 오늘, 넣은 매매가 계좌", c == 200 and r.get("text") == "가짜 답" and r.get("tools") == ["price"]
                  and seen.get("today") == seen.get("today_q") == "2026-11-11" and seen.get("text") == "SPY 종가?"
                  and list(got_trades) == ["SPY"] and [t["q"] for t in got_trades.get("SPY", [])] == [10, 4],
                  f"{c} today={seen.get('today')} 종목={list(got_trades)}"))
    httpd.app.ask_lock.acquire()
    expect("묻기: 앞 질문의 답을 만드는 중", ask(good), 429, "BUSY")
    httpd.app.ask_lock.release()
    # 다른 사이트의 페이지가 보내는 질문(모델 호출에 돈이 듦), 폼 모양 본문, 계좌 모양(6단계 검토 2026-11-17)
    expect("묻기: 다른 사이트에서 보냄", http(base, "/api/ask", good, headers={"Origin": "http://evil.example"}), 403, "BAD_ORIGIN")
    expect("묻기: 같은 화면에서 보냄", http(base, "/api/ask", good, headers={"Origin": f"http://127.0.0.1:{port}"}), 200)
    expect("묻기: 폼 모양 본문", http(base, "/api/ask", good, headers={"Content-Type": "text/plain"}), 415, "BAD_CONTENT_TYPE")
    expect("묻기: 계좌가 목록", ask({**good, "account": [buy("2025-03-04")]}), 400, "BAD_ACCOUNT")
    expect("묻기: 종목 매매가 글", ask({**good, "account": {"SPY": "BUY 10"}}), 400, "BAD_ACCOUNT")
    asked = httpd.app.asks
    httpd.app.asks = screen_server.MAX_ASKS
    expect("묻기: 한 번 띄운 동안 물을 수 있는 수를 넘음", ask(good), 429, "BUSY")
    httpd.app.asks = asked
    httpd.app.ask_fn = broke
    expect("묻기: 모델 호출 상한", ask(good), 503, "BUDGET")

    # 7번 칸(그때 무슨 일이): 모델을 부르지 않는 가짜 함수로 입력 경계, 캐시, 국내 종목, 일정, 잠금(2026-11-17)
    called = []

    def fake_ev(cur, days, rows, sym, day, version, tag=""):
        called.append((sym, day))
        return ({"status": "answered", "head": "머리", "body": "본문 [1]", "text": "머리 본문 [1]",
                 "move": {"pct": 1.0, "ratio": 2.1, "big": True, "judged": True},
                 "citations": [{"n": 1, "id": "x", "title": "t", "publisher": "p", "time": "2026-04-02T16:21:00Z"}]},
                {"llm": [{"cost_usd": 0.01, "error": None}], "seconds": 1.0, "decided_by": None, "window_articles": 9})

    httpd.app.events_fn = fake_ev
    ev = lambda body: http(base, "/api/events", body)
    expect("그날 일: 없는 종목", ev({"symbol": "NOPE", "date": "2026-04-02"}), 400, "UNKNOWN_SYMBOL")
    expect("그날 일: 날짜 모양", ev({"symbol": "TSLA", "date": "20260402"}), 400, "BAD_DATE")
    expect("그날 일: 쉬는 날", ev({"symbol": "TSLA", "date": "2026-04-03"}), 400, "NOT_TRADING_DAY")
    expect("그날 일: 다른 사이트에서 보냄", http(base, "/api/events", {"symbol": "TSLA", "date": "2026-04-02"},
                                         headers={"Origin": "http://evil.example"}), 403, "BAD_ORIGIN")
    expect("그날 일: 폼 모양 본문", http(base, "/api/events", {"symbol": "TSLA", "date": "2026-04-02"},
                                    headers={"Content-Type": "text/plain"}), 415, "BAD_CONTENT_TYPE")
    c1, r1 = ev({"symbol": "TSLA", "date": "2026-04-02"})
    c2, r2 = ev({"symbol": "TSLA", "date": "2026-04-02"})
    others = [o["symbol"] for o in r1.get("others", [])]
    tests.append(("그날 일: 같은 날 다른 자산(자기 종목은 빼고, 코드가 셈)",
                  c1 == 200 and "TSLA" not in others and {"SPY", "TLT", "GLD", "UUP"} <= set(others)
                  and all(isinstance(o.get("pct"), (int, float)) for o in r1["others"]), f"{others}"))
    tests.append(("그날 일: 같은 날 두 번째는 모델을 다시 부르지 않음",
                  c1 == c2 == 200 and r1.get("cached") is False and r2.get("cached") is True and called == [("TSLA", "2026-04-02")],
                  f"{c1} {c2} 부른 수 {len(called)}"))
    c, r = ev({"symbol": "TSLA", "date": "2026-04-06"})
    tests.append(("그날 일: 휴장일(성금요일) 고용 보고서가 다음 거래일 일정에 듦",
                  c == 200 and [e["id"] for e in r.get("schedule", [])] == ["jobs-2026-04-03"], f"{c} {r.get('schedule')}"))
    c, r = ev({"symbol": "005930", "date": "2026-04-02"})
    tests.append(("그날 일: 국내 종목은 모델 없이 고정 문구",
                  c == 200 and r.get("decided_by") == "code:domestic" and "국내 종목은 기사 근거가 없습니다" in (r.get("body") or "")
                  and len(called) == 2, f"{c} {r.get('decided_by')}"))
    last = httpd.app.data.us_days[-1]
    expect("그날 일: 마지막 거래일은 창을 다 채우지 못함", ev({"symbol": "SPY", "date": last}), 400, "NO_WINDOW")
    httpd.app.events_lock.acquire()
    expect("그날 일: 앞 날짜를 정리하는 중", ev({"symbol": "TSLA", "date": "2026-03-31"}), 429, "BUSY")
    httpd.app.events_lock.release()
    stop(httpd)

    # 깨진 저장 파일: 읽기와 저장 모두 500으로 알리고 파일을 덮어쓰지 않음
    broken = SAFE / "f_screen" / "check_api_broken_trades.json"
    broken.write_text('{"symbols": ', encoding="utf-8")
    httpd, _ = serve(screen_data.OUT, 0, broken)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    c, r = http(base, "/api/trades")
    req = urllib.request.Request(base + "/api/trades", method="PUT", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"symbol": "SPY", "asof": "2026-11-11", "trades": []}).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            c2, r2 = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        c2, r2 = e.code, json.loads(e.read())
    stop(httpd)
    kept = broken.read_text(encoding="utf-8") == '{"symbols": '
    broken.unlink()
    tests.append(("깨진 저장 파일", c == 500 and r.get("error") == "SAVED_FILE_BROKEN" and c2 == 500
                  and r2.get("error") == "SAVED_FILE_BROKEN" and kept, f"읽기 {c} 저장 {c2} 파일 그대로 {kept}"))
    n = sum(ok for _, ok, _ in tests)
    print(f"입력 경계: {n}/{len(tests)}")
    for name, ok, got in tests:
        print(f"  {'통과' if ok else '실패'}  {name}: {got}")
    return {"pass": n, "cases": len(tests), "rows": [{"name": a, "pass": b, "got": c} for a, b, c in tests]}


def check_accounts():
    """증권사별 계좌와 지금 잔고(ai/holdings.py). 금액은 출력하지 않고 같음과 코드만 봅니다.
    - 증권사가 둘이면 계좌마다 따로 셈: 계좌 요약 = 그 계좌 줄만 넣은 계산, 실현과 평가와 합계는 계좌 합, 합계는 합친 계산과 같음
    - 지금 잔고: 거꾸로 되짚은 데이터 첫날 보유(손으로 센 값), 앞으로 다시 세면 지금 잔고로 돌아옴, 안내 코드
    - 잘못된 지금 잔고 줄과 증권사는 400, 묻고 답하기에는 시작 보유를 합친 줄로 넘김, 저장 파일에 증권사가 남음"""
    tmp = GEN / "check_accounts_trades.json"
    httpd, _ = serve(screen_data.OUT, 0, trades=tmp)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    pnl = lambda trades, sym="005930", asof="2026-11-11": http(base, "/api/pnl", {"symbol": sym, "asof": asof, "trades": trades})
    row = lambda d, side, q, b="", **kw: {"date": d, "side": side, "qty": q, **({"broker": b} if b else {}), **kw}
    hold = lambda q, p, b="", **kw: {"side": "HOLD", "qty": q, "price": p, **({"broker": b} if b else {}), **kw}
    D = lambda v: Decimal(str(v))
    tests = []

    def ok(name, cond, got=""):
        tests.append((name, bool(cond), got))

    # 1. 국내 종목, 증권사 둘
    t1 = [row("2025-03-04", "BUY", "10", "TOSS"), row("2025-06-02", "BUY", "5", "KAKAOPAY"), row("2025-09-01", "SELL", "3", "KAKAOPAY")]
    c, r = pnl(t1)
    s = r.get("summary") or {}
    acc = {a["broker"]: a for a in r.get("accounts", [])}
    ok("두 증권사: 계좌 둘, 여러 계좌 표시", c == 200 and r["multi"] and list(acc) == ["TOSS", "KAKAOPAY"], f"{c} {list(acc)}")
    sums = {k: sum(a["summary"][k] for a in acc.values()) for k in ("realized_krw", "unrealized_krw", "total_krw")}
    ok("두 증권사: 실현, 평가, 합계는 계좌 합", all(s.get(k) == v for k, v in sums.items()))
    ok("두 증권사: 계좌 합계 = 합친 계산의 합계", s.get("total_krw") == s.get("combined_total_krw"))
    alone_same = True
    for b, a in acc.items():
        c2, r2 = pnl([{k: v for k, v in t.items() if k != "broker"} for t in t1 if t.get("broker") == b])
        alone_same &= c2 == 200 and all(same(r2["summary"][k], a["summary"][k]) for k in EXACT)
        basis = D(a["summary"]["qty_left"]) * D(a["summary"]["avg_cost_local"])
        want = round(float(D(a["summary"]["unrealized_local"]) / basis * 100), 2) if basis else None
        alone_same &= a["holding_return_pct"] == want
    ok("두 증권사: 계좌 요약 = 그 계좌 줄만 넣은 계산, 남은 주식 수익률", alone_same)
    ok("두 증권사: 매도는 그 증권사 평단으로", [x["broker"] for x in r["sells"]] == ["KAKAOPAY"]
       and same(r["sells"][0]["avg_cost_before"], acc["KAKAOPAY"]["summary"]["avg_cost_local"]))
    ok("두 증권사: 평단이 하나가 아님", s.get("avg_cost_local") is None and s.get("holding_return_pct") is None)

    # 2. 계좌별 초과 매도(합치면 팔 수 있어도 그 계좌에는 없음)
    c, r = pnl([row("2025-03-04", "BUY", "5", "TOSS"), row("2025-06-02", "BUY", "1", "KAKAOPAY"),
                row("2025-09-01", "SELL", "3", "KAKAOPAY")])
    ok("계좌별 초과 매도", c == 200 and [(w["code"], w["trade_index"]) for w in r["warnings"]] == [("OVERSOLD", 2)]
       and r["summary"]["total_krw"] == r["summary"]["combined_total_krw"], f"{c} {[w['code'] for w in r.get('warnings', [])]}")

    # 3. 미국 종목 지금 잔고: 기간 전 매수 한 줄, 기간 안 매수 4와 매도 6, 지금 잔고 48주 420달러 1,350원
    t3 = [row("2023-05-01", "BUY", "50", "KIS", price="300", fx_rate="1300"),
          row("2025-03-03", "BUY", "4", "KIS", price="480", fx_rate="1450"),
          row("2025-08-01", "SELL", "6", "KIS", price="560", fx_rate="1390"), hold("48", "420", "KIS", fx_rate="1350")]
    c, r = pnl(t3, "QQQ")
    a = (r.get("accounts") or [{}])[0]
    o = a.get("opening") or {}
    # 손으로 센 값: 되짚어 54주 원가 22,680달러(원화 30,618,000원) → 매수 4 빼면 50주 20,760달러(27,834,000원)
    ok("지금 잔고: 데이터 첫날 보유를 거꾸로 셈(50주, 415.2달러, 환율 27,834,000 / 20,760)",
       c == 200 and o.get("qty") == 50 and same(o.get("avg_cost_local"), 415.2)
       and same(o.get("avg_buy_fx"), float((D(27834000) / D(20760)).quantize(Decimal("0.0001")))), f"{c} {o}")
    ss = a.get("summary") or {}
    ok("지금 잔고: 앞으로 다시 세면 지금 잔고(48주, 420달러, 1,350원)", ss.get("qty_left") == 48
       and abs(D(ss.get("avg_cost_local", 0)) - 420) < Decimal("0.001") and abs(D(ss.get("avg_buy_fx", 0)) - 1350) < Decimal("0.001"))
    codes = sorted(n["code"] for n in r.get("notes", []))
    ok("지금 잔고: 안내(시작 보유, 기간 전 줄 대신, SPY 견주지 않음)",
       codes == ["BENCH_OFF", "HOLD_USED", "PRE_WINDOW_COVERED"] and r["opening"], str(codes))
    st0 = (r.get("steps") or [{}])[0]
    ok("지금 잔고: 장부 첫 단계가 시작 보유(지금 잔고 줄 번호, 데이터 첫날)", st0.get("opening") and st0.get("trade_index") == 3
       and st0.get("date") == r["prices"][0][0])
    # 시험용 체결가(480, 560달러)는 그날 범위 밖이라 1, 2번 줄에는 PRICE_OUT_OF_RANGE가 맞음. 시작 보유(평단)에는 붙이지 않음
    ws = [(w["code"], w["trade_index"]) for w in r["warnings"]]
    ok("지금 잔고: 기간 전 줄은 그대로 OUT_OF_DATA_RANGE, 시작 보유에는 가격 범위 경고 없음",
       [x for x in ws if x[1] in (0, 3)] == [("OUT_OF_DATA_RANGE", 0)], str(ws))
    ok("지금 잔고: 한 계좌라 요약 칸에 남은 주식 수익률", r["summary"].get("holding_return_pct") == a["holding_return_pct"] is not None)

    # 4. 지금 잔고로 셀 수 없는 경우들(시작 보유를 넣지 않고 안내만)
    qq = lambda trades: pnl(trades, "QQQ")
    fx = {"fx_rate": "1400"}
    cases = [
        ("지금 잔고가 기간 안 매수보다 적음", [row("2025-03-03", "BUY", "20", "KIS"), hold("10", "400", "KIS", **fx)], ["HOLD_TOO_FEW"]),
        ("기간 안 매매로 모두 설명됨", [row("2025-03-03", "BUY", "5", "KIS", price="400", **fx), hold("5", "400", "KIS", **fx)],
         ["HOLD_EXPLAINED"]),
        ("모두 설명되나 평단이 다름", [row("2025-03-03", "BUY", "5", "KIS", price="400", **fx), hold("5", "410", "KIS", **fx)],
         ["HOLD_COST_MISMATCH", "HOLD_EXPLAINED"]),
        ("모두 판 뒤 다시 산 것까지 설명됨", [row("2025-03-03", "BUY", "5", "KIS", price="400", **fx),
                                       row("2025-04-01", "SELL", "5", "KIS", price="420", **fx),
                                       row("2025-06-02", "BUY", "3", "KIS", price="450", **fx), hold("3", "450", "KIS", **fx)],
         ["HOLD_EXPLAINED"]),
        ("모두 판 매도 앞의 평단을 모름", [row("2025-04-01", "SELL", "5", "KIS", price="420", **fx),
                                     row("2025-06-02", "BUY", "3", "KIS", price="450", **fx), hold("3", "450", "KIS", **fx)],
         ["HOLD_SOLD_OUT"]),
    ]
    for name, trades, want in cases:
        c, r = qq(trades)
        got = sorted(n["code"] for n in r.get("notes", []))
        ok(f"지금 잔고: {name}", c == 200 and got == want and not r["opening"], f"{c} {got}")

    # 4-2. 기간 전 매매만 있고 지금 잔고 줄이 없으면 그 증권사 첫 기간 전 줄에 안내(11/19 중간 판: 장기 보유가 비어 보임)
    c, r = qq([row("2023-05-01", "BUY", "50", "KIS", price="300", fx_rate="1300"), row("2025-08-01", "SELL", "6", "KIS")])
    ok("지금 잔고 없음: 기간 전 줄에 지금 잔고 넣기 안내", c == 200
       and [(n["code"], n["trade_index"], n["broker"]) for n in r.get("notes", [])] == [("PRE_WINDOW_NEEDS_HOLD", 0, "KIS")]
       and "평균 매입 환율" in r["notes"][0]["message"] and not r["booked"], f"{c} {[n['code'] for n in r.get('notes', [])]}")
    c, r = qq([row("2023-05-01", "BUY", "50", "KIS", price="300", fx_rate="1300"), hold("44", "300", "KIS", **fx)])
    ok("지금 잔고 있음: 지금 잔고 넣기 안내 없음", c == 200 and "PRE_WINDOW_NEEDS_HOLD" not in [n["code"] for n in r.get("notes", [])])

    # 5. 잘못된 지금 잔고 줄과 증권사
    bad = [("지금 잔고 환율 없음(미국)", [hold("3", "450", "KIS")]),
           ("지금 잔고 둘(같은 증권사)", [hold("3", "450", "KIS", **fx), hold("2", "450", "KIS", **fx)]),
           ("모르는 증권사", [row("2025-03-03", "BUY", "5", "FOO")]),
           ("지금 잔고 수량 0", [hold("0", "450", "KIS", **fx)]),
           ("지금 잔고 소수 수량", [hold("1.5", "450", "KIS", **fx)]),
           ("지금 잔고 평균 단가 없음", [{"side": "HOLD", "qty": "3", "fx_rate": "1400"}])]
    for name, trades in bad:
        c, r = qq(trades)
        ok(f"400: {name}", c == 400 and r.get("error") == "BAD_TRADE", f"{c} {r.get('error')}")

    # 6. 증권사 없는 한 계좌는 전과 같은 한 번의 계산(계좌 하나, 빈 증권사)
    c, r = pnl([row("2025-03-04", "BUY", "10")])
    ok("증권사 없음: 계좌 하나, 여러 계좌 아님, 시작 보유 없음", c == 200 and not r["multi"] and not r["opening"]
       and [a["broker"] for a in r["accounts"]] == [""] and r["summary"]["holding_return_pct"] == r["accounts"][0]["holding_return_pct"])

    # 7. 묻고 답하기에는 시작 보유를 합친 줄(지금 잔고 줄과 증권사 칸은 빠짐)
    seen = {}

    def fake(ts, q):
        seen["trades"] = ts.account["trades"]
        return ({"status": "answered", "text": "가짜 답", "numbers": [], "dates_used": [], "warnings": []},
                {"tool_calls": [], "llm": [], "decided_by": None})
    httpd.app.ask_fn = fake
    c, r = http(base, "/api/ask", {"question": "QQQ 손익?", "asof": "2026-11-11", "account": {"QQQ": t3}})
    got = seen.get("trades", {}).get("QQQ", [])
    ok("묻기: 시작 보유가 맨 앞 매수(50주), 지금 잔고 줄과 증권사 칸 없음", c == 200 and got and got[0]["side"] == "BUY"
       and got[0]["q"] == 50 and all(t["side"] != "HOLD" and "broker" not in t for t in got)
       and [t["q"] for t in got[1:]] == [4, 6], f"{c} {[(t['side'], t['q']) for t in got]}")

    # 8. 저장 파일에 증권사와 지금 잔고 줄이 그대로 남음
    req = urllib.request.Request(base + "/api/trades", method="PUT", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"symbol": "QQQ", "asof": "2026-11-11", "trades": t3[1:]}).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            c = resp.status
    except urllib.error.HTTPError as e:
        c = e.code
    saved = json.loads(tmp.read_text(encoding="utf-8")) if tmp.exists() else {}
    rows_saved = saved.get("symbols", {}).get("QQQ", {}).get("trades", [])
    ok("저장: 증권사와 지금 잔고 줄이 남음", c == 200 and [t.get("broker") for t in rows_saved] == ["KIS"] * 3
       and rows_saved[-1].get("side") == "HOLD" and "date" not in rows_saved[-1])
    stop(httpd)
    tmp.unlink(missing_ok=True)
    n = sum(1 for _, b, _ in tests if b)
    print(f"증권사와 지금 잔고: {n}/{len(tests)}")
    for name, b, got in tests:
        print(f"  {'통과' if b else '실패'}  {name}" + (f": {got}" if not b and got else ""))
    return {"pass": n, "cases": len(tests), "rows": [{"name": a, "pass": b} for a, b, _ in tests]}


def check_portfolio():
    """전체 탭. 종목마다 손익 API(/api/pnl)를 따로 불러 더한 값과, 화면 데이터 파일에서 따로 읽은 기준일 종가와 매매기준율로 센
    평가액과 견줍니다. 금액은 출력하지 않습니다."""
    httpd, _ = serve(screen_data.OUT, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    asof = "2026-11-11"
    row = lambda d, side, q, b="", **kw: {"date": d, "side": side, "qty": q, **({"broker": b} if b else {}), **kw}
    acct = {"QQQ": [row("2025-03-03", "BUY", "3", "TOSS"), row("2025-08-01", "SELL", "1", "TOSS"),
                    row("2025-06-02", "BUY", "2", "KIS"), {"side": "HOLD", "qty": "11", "price": "410", "fx_rate": "1360", "broker": "KIS"}],
            "005930": [row("2025-03-04", "BUY", "10"), row("2025-06-02", "BUY", "5", "KAKAOPAY"), row("2025-09-01", "SELL", "2", "KAKAOPAY")],
            "TSLA": [row("2024-06-03", "BUY", "60", "KIS", price="250.00", fx_rate="1300.00"), row("2025-06-02", "SELL", "30", "KIS")],
            "SPY": []}
    tests = []

    def ok(name, cond, got=""):
        tests.append((name, bool(cond), got))

    c, r = http(base, "/api/portfolio", {"asof": asof, "account": acct})
    ok("200과 종목 셋(빈 종목은 뺌)", c == 200 and sorted(x["symbol"] for x in r["symbols"]) == ["005930", "QQQ", "TSLA"], f"{c}")
    keys = ("realized_krw", "unrealized_krw", "total_krw", "invested_krw")
    each = {}
    for sym, rows in acct.items():
        if rows:
            c2, r2 = http(base, "/api/pnl", {"symbol": sym, "asof": asof, "trades": rows})
            each[sym] = r2
    ok("종목 줄 = 그 종목 손익 API", all(all(x[k] == (each[x["symbol"]]["summary"] or {}).get(k, 0) for k in keys)
                                    for x in r["symbols"]))
    ok("전체 = 종목 손익 API의 합", all(r["total"][k] == sum((v["summary"] or {}).get(k, 0) for v in each.values()) for k in keys))
    # 평가액: 화면 데이터 파일에서 따로 읽은 기준일 이전 마지막 종가와 그날(달력에 맞춘 날 이전 마지막) 매매기준율
    con = duckdb.connect(str(screen_data.OUT), read_only=True)
    end = con.execute("select max(cal_date) from calendar where cal_date <= ?", [asof]).fetchone()[0]
    fx = con.execute("select rate from fx where rate_date = (select max(rate_date) from fx where rate_date <= ?)", [end]).fetchone()[0]
    good_value = True
    for x in r["symbols"]:
        close = con.execute("select close from prices where symbol = ? and trade_date = (select max(trade_date) from prices "
                            "where symbol = ? and trade_date <= ?)", [x["symbol"], x["symbol"], asof]).fetchone()[0]
        rate = fx if x["currency"] == "USD" else 1
        want = sum(int((Decimal(a["qty_left"]) * close * rate).quantize(Decimal(1), rounding="ROUND_HALF_UP")) for a in x["accounts"])
        good_value &= x["value_krw"] == want
    con.close()
    ok("평가액 = 남은 수량 x 기준일 종가 x 매매기준율(계좌마다 원 단위)", good_value)
    ok("비중 합 100%(반올림 0.05 안)", abs(sum(x["weight_pct"] or 0 for x in r["symbols"]) - 100) < 0.05)
    br = {b["broker"]: b for b in r["brokers"]}
    sums = {}
    for x in r["symbols"]:
        for a in x["accounts"]:
            sb = sums.setdefault(a["broker"], {k: 0 for k in keys})
            for k in keys:
                sb[k] += a[k]
    ok("증권사별 = 계좌들의 합", set(br) == set(sums) and all(br[b][k] == sums[b][k] for b in sums for k in keys), str(sorted(br)))
    ok("증권사별 합의 합 = 전체", all(sum(b[k] for b in r["brokers"]) == r["total"][k] for k in keys))
    tsla = next(x for x in r["symbols"] if x["symbol"] == "TSLA")
    ok("기간 전 매매만 있는 종목: 계산 안 됨과 지금 잔고 안내", not tsla["booked"] and "PRE_WINDOW_NEEDS_HOLD" in tsla["notes"]
       and tsla["brokers"] == ["한국투자증권"])
    # 틀린 줄이 있는 종목은 빼고 까닭을 남김(전체가 실패하지 않음), 잘못된 입력은 400
    c, r = http(base, "/api/portfolio", {"asof": asof, "account": {**acct, "NVDA": [{"side": "HOLD", "qty": "1", "broker": "KIS"}]}})
    ok("틀린 줄이 있는 종목은 빼고 까닭", c == 200 and [x["symbol"] for x in r["skipped"]] == ["NVDA"]
       and "평균 단가가 없습니다" in r["skipped"][0]["message"], f"{c} {r.get('skipped')}")
    for name, body, code in (("기준일 틀림", {"asof": "2026-13-01", "account": acct}, "BAD_DATE"),
                             ("계좌가 목록", {"asof": asof, "account": []}, "BAD_ACCOUNT"),
                             ("없는 종목", {"asof": asof, "account": {"NOPE": []}}, "UNKNOWN_SYMBOL")):
        c, r = http(base, "/api/portfolio", body)
        ok(f"400: {name}", c == 400 and r.get("error") == code, f"{c} {r.get('error')}")
    c, r = http(base, "/api/portfolio", {"asof": asof, "account": {}})
    ok("빈 계좌: 0과 빈 목록", c == 200 and r["symbols"] == [] and r["total"]["total_krw"] == 0 and r["total"]["simple_return_pct"] is None)
    stop(httpd)
    n = sum(1 for _, b, _ in tests if b)
    print(f"전체 탭: {n}/{len(tests)}")
    for name, b, got in tests:
        print(f"  {'통과' if b else '실패'}  {name}" + (f": {got}" if not b and got else ""))
    return {"pass": n, "cases": len(tests), "rows": [{"name": a, "pass": b} for a, b, _ in tests]}


CHECKS = {"golden": check_golden, "real": check_real, "latency": check_latency, "pipeline": check_pipeline,
          "edges": check_edges, "accounts": check_accounts, "portfolio": check_portfolio}


def passed(k, v):
    if k == "real":
        return v["real_pass"] == v["real"] and v["warn_pass"] == v["warn"]
    if k == "latency":
        return v["pass"]
    if k == "pipeline":
        return v["pass"] == v["breaks"] and v["control_ok"]
    return v["pass"] == v["cases"]


if __name__ == "__main__":
    which = sys.argv[1:] or list(CHECKS)
    result = {w: CHECKS[w]() for w in which}
    GEN.mkdir(parents=True, exist_ok=True)
    path = GEN / "api_check.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    old.update({k: {**v, "checked": time.strftime("%Y-%m-%dT%H:%M:%S")} for k, v in result.items()})
    path.write_text(json.dumps(old, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    ok = all(passed(k, v) for k, v in result.items())
    print("\n대조:", "모두 통과" if ok else "실패 있음")
    sys.exit(0 if ok else 1)
