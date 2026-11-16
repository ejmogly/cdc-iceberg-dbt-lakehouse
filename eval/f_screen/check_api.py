#!/usr/bin/env python3
"""F 화면 계산 서버를 평가 기준(docs/ai/F-screen.md 4절)으로 대조합니다. 서버를 같은 프로세스에 띄워 HTTP로 부릅니다.

사용: .venv/bin/python eval/f_screen/check_api.py            다섯 모두
      .venv/bin/python eval/f_screen/check_api.py golden     손익 정확도(골든) 30개
      .venv/bin/python eval/f_screen/check_api.py real       손익 정확도(실제 데이터) 12개, 입력 경고 8개, 새로 짠 계산
      .venv/bin/python eval/f_screen/check_api.py latency    계산 지연(매매 20줄 사례 200번, p95)
      .venv/bin/python eval/f_screen/check_api.py pipeline   파이프라인 상태(깬 원천, 깬 파일)
      .venv/bin/python eval/f_screen/check_api.py edges      입력 경계(잘못된 날짜와 수, 빠진 줄만 있는 매매, Host 머리)

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

from ai import screen_data  # noqa: E402
from ai.screen_server import serve, stop  # noqa: E402

E01 = ROOT / "eval" / "e01_pnl"
HERE = Path(__file__).parent
GEN = ROOT / "data" / "generated" / "f_screen"
EXACT = ["qty_left", "avg_cost_local", "avg_buy_fx", "realized_local", "unrealized_local", "total_local",
         "realized_krw", "unrealized_krw", "total_krw", "invested_krw", "simple_return_pct", "price_effect_krw",
         "fx_effect_krw", "cross_effect_krw", "bench_total_krw", "max_drawdown_krw", "naive_krw"]
NEAR = ["irr_pct", "bench_irr_pct"]
DROPPED_BY_SQL = {"OVERSOLD", "AFTER_ASOF", "BAD_QTY"}


def http(base, path, body=None, host=None):
    headers = {"Content-Type": "application/json"}
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
        "원천 행 하나 빠짐": ({k: v for k, v in raw_prices.items() if k != keys[len(keys) // 2]}, raw_fx,
                       "원천과 원본의 시세 행 수"),
        "원천 환율 하루 빠짐": (raw_prices, {d: r for d, r in raw_fx.items() if d != sorted(raw_fx)[100]},
                         "환율 대조(원본과 하루씩)"),
        "원천 종가 하나 다름": ({k: (v[:3] + ((v[3] * Decimal("1.01")).quantize(Decimal("0.0001")),) + v[4:])
                           if k == keys[len(keys) // 3] else v for k, v in raw_prices.items()}, raw_fx,
                          "시세 값 대조(원본과 한 행씩)"),
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
    print(f"파이프라인 상태: 깬 것 {sum(o['pass'] for o in out)}/{len(out)}을 잡고 손익 API가 503과 실패 항목을 돌려줌, "
          f"깨지 않은 파일은 {'통과와 200' if control else '실패'}")
    for o in out:
        print(f"  {'통과' if o['pass'] else '실패'}  {o['break']}: 상태 {o['status_ok']}, 손익 API {o['pnl_http']}, "
              f"실패 항목 {o['failures']}")
    return {"pass": sum(o["pass"] for o in out), "breaks": len(out), "control_ok": control, "rows": out}


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
    stop(httpd)
    n = sum(ok for _, ok, _ in tests)
    print(f"입력 경계: {n}/{len(tests)}")
    for name, ok, got in tests:
        print(f"  {'통과' if ok else '실패'}  {name}: {got}")
    return {"pass": n, "cases": len(tests), "rows": [{"name": a, "pass": b, "got": c} for a, b, c in tests]}


CHECKS = {"golden": check_golden, "real": check_real, "latency": check_latency, "pipeline": check_pipeline,
          "edges": check_edges}


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
