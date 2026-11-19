#!/usr/bin/env python3
"""F 화면 8번 칸(앞으로의 범위)을 평가 기준대로 잽니다(docs/ai/F-screen.md 4절 「범위 칸」, 평가셋 「범위」).

사용(저장소 루트에서): .venv/bin/python eval/f_screen/check_range.py

1. 정답 대조: 계산 서버의 GET /api/range를 19종목 x 기준일 둘(마지막 날, 1년 전)로 불러, 독립 정답
   (data/generated/f_screen/expected_range.json, eval/f_screen/range_expected.py가 원본을 직접 읽어 냄)과 비교합니다.
   범위와 창마다의 로그 값 차이 1e-6 안, n과 맞은 수와 창마다의 맞음, 표시 갈래가 모두 같고, 보일 값(범위 %는 소수
   둘째 자리, 적중률은 소수 첫째 자리)도 같아야 합니다. 1년 전 기준일은 달력 날짜(마지막 날 - 365일)로 불러, 서버가
   그 이전 마지막 거래일로 맞추는지도 봅니다.
2. 미래 바꾸기 시험: 서버 쪽 계산(ai/screen_range.py)에 종목마다 1년 전 기준일과 적중률을 세는 지난 창의 예측
   날짜마다, 그 날 뒤의 가격을 바꾼 사본(1.5배, 무작위)을 넣어도 범위와 창이 그대로인지 봅니다. 마지막 거래일은 뒤
   자료가 없어 늘 같으므로 세지 않습니다. 같은 시험이 하루 어긋난 창(누수)을 실제로 잡는지 누수 심은 판으로 함께 봅니다.
3. 입력 경계: 없는 종목, 틀린 날짜, 데이터 기간 밖 기준일은 400.
4. 모든 날짜 대조: 19종목 x 데이터 기간의 모든 달력 날짜(주말과 휴장일 포함)로 서버의 범위 계산(app.api_range)을 부르고,
   range_expected.py의 함수(원본을 직접 읽음)로 따로 낸 값과 견줍니다. 쉬는 날 기준일에 앞 거래일을 고르는지(뒤 거래일을
   고르면 미래 누수), 데이터 첫머리(수익률 20개가 없음, 지난 창 0개), 보일 값과 알림 방향까지 봅니다(5단계 독립 검토
   2026-11-17 중간 1번: 정답 38행은 모두 거래일이라 쉬는 날 갈래를 거치지 않았음).
5. 화면 대조: 헤드리스 Chrome에서 종목과 기준일을 38행과 첫머리 두 행대로 바꿔 가며, 8번 칸의 글자(단위, 범위 %, 기준일
   문장, n과 적중률, 참고 문구, 알림이나 표본 문구)가 독립 정답으로 따로 만든 기대 글자와 같은지, 고정 문구(원화 범위
   아님, 다른 방법보다 낫다는 것은 재지 않음)가 있는지 봅니다. 화면의 표시 갈래가 정답의 display와 같은지가 여기서 갈립니다.

--selftest: 서버에 쉬는 날 다음 거래일을 고르는 잘못을 심어 4번이 잡는지, 화면 사본에 잘못(범위 뒤바꿈, 알림 숨김, n
자리에 맞은 수, 단위, 화면에서 다시 반올림, 참고 문구 뺌, 알림 방향 뒤바꿈, 원화 아님 문구 뺌)을 하나씩 심어 5번이
잡는지 봅니다.

가격과 범위 값은 출력하지 않습니다(재배포 금지). 결과는 data/generated/f_screen/range_check.json(git 제외).
"""
import argparse
import json
import math
import random
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai import screen_data, screen_range, screen_server  # noqa: E402
from ai.screen_server import BadRequest, serve, stop  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_flows import CALC_DONE, chrome, type_in  # noqa: E402  헤드리스 Chrome과 DevTools 프로토콜
from check_screen import fpct  # noqa: E402  화면 코드와 따로 만든 표시 규칙(% 부호, 자리)
import range_expected as rx  # noqa: E402  독립 정답의 정의(원본을 직접 읽음)

EXPECTED = ROOT / "data" / "generated" / "f_screen" / "expected_range.json"
OUT = ROOT / "data" / "generated" / "f_screen" / "range_check.json"
TOL = 1e-6
COVER = (0.70, 0.90)  # 2절 8번 칸: 20일 적중률 70 ~ 90% 밖이면 알림
SMALL = 20            # n이 이보다 작으면 표본 문구


def hu(x, places):
    """보일 값의 반올림(0.5는 0에서 먼 쪽). 손익 %와 같은 규칙."""
    return float(Decimal(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def rate_pct(hits, n):
    return hu(Decimal(hits) * 100 / Decimal(n), 1) if n else None


def judge(n, hits):
    """(표시 갈래, 알림 방향). 2절 8번 칸의 규칙을 따로 적은 것."""
    if n < SMALL:
        return "표본 문구", None
    rate = hits / n
    if COVER[0] <= rate <= COVER[1]:
        return "없음", None
    return "알림", ("넓음" if rate > COVER[1] else "좁음")


def get(port, query):
    url = f"http://127.0.0.1:{port}/api/range?{query}"
    req = urllib.request.Request(url, headers={"Origin": f"http://127.0.0.1:{port}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def compare(api, row):
    """한 행의 다른 점 목록과 (가장 큰 로그 값 차이)."""
    bad, worst = [], 0.0

    def close(where, a, b):
        nonlocal worst
        if a is None or b is None:
            if a is not b:
                bad.append(f"{where}: {a!r} / {b!r}")
            return
        worst = max(worst, abs(a - b))
        if abs(a - b) > TOL:
            bad.append(f"{where} 차이 {abs(a - b):.2e}")

    if api["base_date"] != row["date"]:
        bad.append(f"기준일 {api['base_date']} / 정답 {row['date']}")
    close("범위 아래", api["lo_log"], row["lo_log"])
    close("범위 위", api["hi_log"], row["hi_log"])
    for k in ("n", "hits", "display"):
        if api[k] != row[k]:
            bad.append(f"{k} {api[k]!r} / 정답 {row[k]!r}")
    if len(api["windows"]) == len(row["windows"]):
        for a, b in zip(api["windows"], row["windows"]):
            if a["date"] != b["date"] or a["hit"] != b["hit"]:
                bad.append(f"창 {b['date']}: 날짜나 맞음이 다름")
            for k in ("lo_log", "hi_log", "actual_log"):
                close(f"창 {b['date']} {k}", a[k], b[k])
    else:
        bad.append(f"창 수 {len(api['windows'])} / 정답 {len(row['windows'])}")
    # 보일 값: 범위 %는 소수 둘째 자리, 적중률은 소수 첫째 자리(4절). 서버 값 자체를 견줌(다시 서식하지 않음)
    for k in ("lo_pct", "hi_pct"):
        if api[k] != hu(row[k], 2):
            bad.append(f"{k} 보일 값 {api[k]!r} / 정답 {hu(row[k], 2)!r}")
    if api["hit_rate_pct"] != rate_pct(row["hits"], row["n"]):
        bad.append(f"적중률 보일 값 {api['hit_rate_pct']!r} / 정답 {rate_pct(row['hits'], row['n'])!r}")
    if api["side"] != judge(row["n"], row["hits"])[1]:
        bad.append(f"알림 방향 {api['side']!r} / 정답 {judge(row['n'], row['hits'])[1]!r}")
    return bad, worst


def sweep(app):
    """모든 날짜 대조. {"calls", "rejected", "bad": [..]}. 서버 함수는 app.api_range를 프로세스 안에서 부릅니다."""
    cal = app.data.calendar
    first, last = date.fromisoformat(cal[0]), date.fromisoformat(cal[-1])
    dates = [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]
    out = {"calls": 0, "rejected": 0, "bad": []}
    for ins in rx.instruments():
        sym, market = ins["symbol"], ins["market"]
        days, px, _ = rx.load(sym, market)
        iso = [d.isoformat() for d in days]
        ptr = -1  # 기준일 이하 마지막 거래일의 순번. 날짜가 오름차순이라 앞으로만 감
        for d in dates:
            while ptr + 1 < len(iso) and iso[ptr + 1] <= d:
                ptr += 1
            out["calls"] += 1
            try:
                api = app.api_range({"symbol": [sym], "asof": [d]})
            except BadRequest:
                out["rejected"] += 1
                if ptr >= 0:
                    out["bad"].append(f"{sym} {d}: 400인데 앞 거래일 {iso[ptr]}이 있음")
                continue
            if ptr < 0:
                out["bad"].append(f"{sym} {d}: 앞 거래일이 없는데 200")
                continue
            b = rx.band(px, ptr)
            ws = rx.windows(px, ptr)
            hits = sum(w[4] for w in ws)
            display, side = judge(len(ws), hits)
            want = {"base_date": iso[ptr], "ok": b is not None, "n": len(ws), "hits": hits, "display": display, "side": side,
                    "lo_pct": hu((math.exp(b[0]) - 1) * 100, 2) if b else None,
                    "hi_pct": hu((math.exp(b[1]) - 1) * 100, 2) if b else None,
                    "hit_rate_pct": rate_pct(hits, len(ws)),
                    "win": [(iso[w[0]], w[4]) for w in ws]}
            got = {k: api[k] for k in want if k != "win"}
            got["win"] = [(w["date"], w["hit"]) for w in api["windows"]]
            diff = [k for k in want if got[k] != want[k]]
            if b and (abs(api["lo_log"] - b[0]) > TOL or abs(api["hi_log"] - b[1]) > TOL):
                diff.append("로그 범위")
            if diff:
                out["bad"].append(f"{sym} {d}: " + ", ".join(f"{k} {got[k]!r} / {want[k]!r}" if k in got else k for k in diff[:2]))
    return out


def leaky_band(px, t):
    """누수 심은 판: 수익률 창을 하루 밀어 t 다음 날의 수익률까지 씀(흔한 하루 어긋남)."""
    if t < screen_range.PAST - 1:
        return None
    sq = sum(math.log(px[i] / px[i - 1]) ** 2 for i in range(t - screen_range.PAST + 2, t + 2))
    half = screen_range.Z * (math.sqrt(sq / screen_range.PAST) + screen_range.GUARD) * math.sqrt(screen_range.AHEAD)
    return -half, half


def perturb(px, p, mode, rng):
    if mode == "1.5배":
        return px[:p + 1] + [c * 1.5 for c in px[p + 1:]]
    out = px[:p + 1]
    for _ in range(len(px) - p - 1):
        out.append(out[-1] * math.exp(rng.gauss(0, 0.03)))
    return out


def future_test(app, rows):
    """미래 바꾸기 시험. 서버가 읽은 가격 목록(app.px)에 screen_range를 직접 부릅니다."""
    rng = random.Random(20261117)
    out = {"실제 판": {"points": 0, "changed": 0, "windows_changed": 0},
           "누수 심은 판": {"points": 0, "changed": 0, "symbols_missed": []}}
    real_band = screen_range.band
    for sym in sorted({r["symbol"] for r in rows}):
        days = app.data.rows[sym]
        px = app.px[sym]
        bases = [days.index(r["date"]) for r in rows if r["symbol"] == sym]
        points = set(bases) | {w[0] for t in bases for w in screen_range.past(px, t)}
        points = sorted(p for p in points if p < len(px) - 1)
        for name, fn in (("실제 판", real_band), ("누수 심은 판", leaky_band)):
            changed = 0
            for mode in ("1.5배", "무작위"):
                for p in points:
                    changed += fn(px, p) != fn(perturb(px, p, mode, rng), p)
                out[name]["points"] += len(points)
            out[name]["changed"] += changed
            if name == "누수 심은 판" and not changed:
                out[name]["symbols_missed"].append(sym)
        # 1년 전 기준일의 창 전체(범위, 실제 값, 맞음)도 기준일 뒤 값과 무관해야 함
        for t in bases:
            if t >= len(px) - 1:
                continue
            for mode in ("1.5배", "무작위"):
                if screen_range.view(days, px, t) != screen_range.view(days, perturb(px, t, mode, rng), t):
                    out["실제 판"]["windows_changed"] += 1
    ok = (out["실제 판"]["changed"] == 0 and out["실제 판"]["windows_changed"] == 0
          and not out["누수 심은 판"]["symbols_missed"])
    return ok, out


def main():
    exp = json.loads(EXPECTED.read_text(encoding="utf-8"))
    rows = exp["rows"]
    httpd, _ = serve(screen_data.eval_db(), 0, None)
    port = httpd.server_address[1]
    try:
        results, worst, ms = [], 0.0, []
        for r in rows:
            status, api = get(port, f"symbol={r['symbol']}&asof={r['date']}")
            if status != 200:
                results.append({"symbol": r["symbol"], "base": r["base"], "ok": False, "bad": [f"HTTP {status} {api}"]})
                continue
            bad, w = compare(api, r)
            worst = max(worst, w)
            ms.append(api["ms"])
            if r["base"] == "1년 전":  # 달력 날짜로 불러도 같은 거래일로 맞추는지
                last = date.fromisoformat(exp["sources"][r["symbol"]]["last"])
                st2, api2 = get(port, f"symbol={r['symbol']}&asof={(last - timedelta(days=365)).isoformat()}")
                if st2 != 200 or api2["base_date"] != r["date"]:
                    bad.append(f"달력 날짜로 부른 기준일 {api2.get('base_date') if st2 == 200 else st2} / 정답 {r['date']}")
            results.append({"symbol": r["symbol"], "base": r["base"], "ok": not bad, "bad": bad})
        # 경계에 가장 가까운 창(계산 차이로 맞음 판정이 뒤집힐 여유)
        margin = min(min(abs(w["actual_log"] - w["lo_log"]), abs(w["actual_log"] - w["hi_log"]))
                     for r in rows for w in r["windows"])

        ok_future, future = future_test(httpd.app, rows)

        edges = []
        for q, want in (("symbol=NOPE&asof=2026-11-12", "UNKNOWN_SYMBOL"), ("symbol=QQQ&asof=2026-13-01", "BAD_DATE"),
                        ("symbol=QQQ&asof=2030-01-01", "OUT_OF_DATA_RANGE"), ("symbol=QQQ", "BAD_DATE")):
            st, body = get(port, q)
            edges.append({"query": q, "ok": st == 400 and body.get("error") == want, "got": [st, body.get("error")]})

        swept = sweep(httpd.app)

        screen = screen_pass(port, rows + dev_rows())

        n_ok = sum(x["ok"] for x in results)
        for x in results:
            if not x["ok"]:
                print(f"  다름  {x['symbol']} {x['base']}: {'; '.join(x['bad'][:4])}")
        print(f"정답 대조: {n_ok}/{len(results)}(19종목 x 기준일 둘), 로그 값의 가장 큰 차이 {worst:.2e}(허용 {TOL:.0e}), "
              f"경계에 가장 가까운 창 {margin:.2e}, 계산 {(str(max(ms)) + "ms 이하") if ms and max(ms) else "1ms 미만"}")
        r, k = future["실제 판"], future["누수 심은 판"]
        print(f"미래 바꾸기: 실제 판 시험 {r['points']}번 가운데 범위가 바뀐 것 {r['changed']}, 기준일 창 전체가 바뀐 것 "
              f"{r['windows_changed']} / 누수 심은 판 {k['points']}번 가운데 {k['changed']}번 바뀜, 놓친 종목 "
              f"{', '.join(k['symbols_missed']) or '없음'}")
        print(f"입력 경계: {sum(e['ok'] for e in edges)}/{len(edges)}")
        for x in swept["bad"][:5]:
            print(f"  다름  {x}")
        print(f"모든 날짜 대조: {swept['calls'] - len(swept['bad'])}/{swept['calls']} 같음(19종목 x 달력 날짜, 앞 거래일이 없어 400인 것 "
              f"{swept['rejected']})")
        s_ok = sum(not b for b in screen.values())
        for k, b in screen.items():
            if b:
                print(f"  화면 다름  {k}: {'; '.join(b[:3])}")
        print(f"화면 대조: {s_ok}/{len(screen)}(8번 칸 글자와 표시 갈래, 정답 38행과 첫머리 두 행, 고정 문구)")
        ok = (n_ok == len(results) and ok_future and all(e["ok"] for e in edges) and not swept["bad"]
              and s_ok == len(screen))
        OUT.write_text(json.dumps({"results": results, "worst_log_diff": worst, "closest_window": margin,
                                   "future": future, "edges": edges, "sweep": swept, "screen": screen, "ok": ok},
                                  ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print("범위 칸 대조 → " + ("통과" if ok else "실패"))
        return 0 if ok else 1
    finally:
        stop(httpd)


# ---------- 화면 대조 ----------

def dev_rows():
    """데이터 첫머리 두 행(정답 38행에 없는 갈래): 수익률 20개가 없어 범위를 셀 수 없음, 범위는 있지만 지난 창이 0개.
    range_expected.py의 함수로 따로 냅니다."""
    out = []
    ins = next(i for i in rx.instruments() if i["symbol"] == "QQQ")
    days, px, _ = rx.load("QQQ", ins["market"])
    for base, t in (("첫머리(범위 없음)", 10), ("첫머리(지난 창 0)", 30)):
        b = rx.band(px, t)
        ws = rx.windows(px, t)
        hits = sum(w[4] for w in ws)
        out.append({"symbol": "QQQ", "market": ins["market"], "base": base, "date": days[t].isoformat(),
                    "lo_pct": rx.pct(b[0]) if b else None, "hi_pct": rx.pct(b[1]) if b else None,
                    "n": len(ws), "hits": hits, "hit_rate": hits / len(ws) if ws else None,
                    "display": judge(len(ws), hits)[0]})
    return out


def want_screen(r):
    """정답 한 행에서 8번 칸의 기대 글자. 화면 코드를 쓰지 않고 같은 규칙(4절: 범위 %는 소수 둘째 자리, 적중률은 소수 첫째
    자리, 0.5는 0에서 먼 쪽. 2절: n < 20이면 표본 문구, 70 ~ 90% 밖이면 알림, 2년이라 참고로만)으로 따로 만듭니다."""
    unit = "원" if r["market"] == "KRX" else "달러"
    display, side = judge(r["n"], r["hits"])
    ok = r["lo_pct"] is not None
    return {
        "unit": f"{unit} 가격, 앞으로 20거래일",
        "range": f"{fpct(hu(r['lo_pct'], 2))} ~ {fpct(hu(r['hi_pct'], 2))}" if ok else "–",
        "sub": (f"{r['date']} 종가에서 20거래일 뒤까지 가격 변화의 80% 범위입니다. 최근 20일 변동성이 이어진다고 본 단순 계산입니다."
                if ok else f"{r['date']} 앞에 일별 수익률 20개가 없어 범위를 셀 수 없습니다."),
        "hit": (f"지난 {r['n']}번의 20거래일 창에서 이 방법의 범위 안에 든 것은 {r['hits']}번({rate_pct(r['hits'], r['n']):.1f}%)입니다. "
                f"데이터가 2년뿐이라 참고로만 보는 값입니다." if r["n"] else "지난 창이 없어 적중률을 셀 수 없습니다."),
        "flag": (None if display == "없음" or not r["n"]
                 else f"표본이 적어(n = {r['n']}) 적중률로 판단하기 어렵습니다." if display == "표본 문구"
                 else "적중률이 90%를 넘어, 이 종목에서는 80% 범위가 지난날의 움직임보다 넓었습니다." if side == "넓음"
                 else "적중률이 70%에 못 미쳐, 이 종목에서는 80% 범위가 지난날의 움직임보다 좁았습니다."),
    }


READ = """(() => { const t = (i) => document.getElementById(i).textContent; const f = document.getElementById('rg-flag');
  return {unit: t('rg-unit'), range: t('rg-range'), sub: t('rg-sub'), hit: t('rg-hit'),
          flag: f.hidden ? null : t('rg-flag'), stale: !document.getElementById('rg-stale').hidden}; })()"""
# 2절 8번 칸이 화면에 밝히라고 한 고정 문구
FIXED = ("원화 범위가 아닙니다", "다른 방법보다 낫다는 것은 재지 않았습니다", "이 방법의 범위 안에 든 것")


def screen_pass(port, rows):
    """한 Chrome에서 종목과 기준일을 행마다 바꿔 8번 칸 글자를 읽어 대조. {이름: 다른 점 목록}."""
    out = {}
    proc, prof, page = chrome()
    try:
        page.call("Runtime.enable")
        page.call("Page.enable")
        page.call("Page.navigate", url=f"http://127.0.0.1:{port}/")
        if not page.wait("!document.getElementById('range-box').hidden && !!document.getElementById('range-box').dataset.base", 20):
            return {"처음 범위": ["8번 칸이 뜨지 않음"]}
        # 예시 매매의 첫 손익 계산(서버를 띄운 직후라 몇 초 걸림)이 끝난 뒤 시작합니다. 계산 중에 서버를 끄지 않게
        page.wait(CALC_DONE, 20)
        text = page.js("document.getElementById('range-box').textContent")
        out["고정 문구"] = [f"없음: {f}" for f in FIXED if f not in text]
        for r in rows:
            name = f"{r['symbol']} {r['base']}"
            if page.js("document.getElementById('sym').value") != r["symbol"]:
                type_in(page, "sym", r["symbol"], "change")
            type_in(page, "asof", r["date"], "change")
            done = (f"(() => {{ const b = document.getElementById('range-box'); return !b.hidden && !b.classList.contains('stale')"
                    f" && b.dataset.symbol === {json.dumps(r['symbol'])} && b.dataset.base === {json.dumps(r['date'])}; }})()")
            if not page.wait(done, 5):
                out[name] = ["8번 칸이 이 종목과 기준일로 바뀌지 않음"]
                continue
            got, want = page.js(READ), want_screen(r)
            bad = [f"{k}: 화면 {got[k]!r}, 기대 {v!r}" for k, v in want.items() if got[k] != v]
            if got["stale"]:
                bad.append("흐림 안내가 남음")
            if page.errors:
                bad.append(f"잡히지 않은 오류: {page.errors}")
            out[name] = bad
        # 끄기 전에 화면을 비워, 서버가 처리 중인 요청을 마치게 합니다(끊긴 연결에 답을 쓰지 않게)
        page.call("Page.navigate", url="about:blank")
        page.pump(0.5)
    finally:
        proc.kill()
        proc.wait()
        prof.cleanup()
    return out


# 자체 시험: 화면 사본에 잘못을 하나씩 심어 화면 대조가 잡는지 봅니다. (이름, 파일, 찾을 글, 바꿀 글)
BUGS = [
    ("범위 아래와 위를 뒤바꿈", "app.js", "${pct(v.lo_pct)} ~ ${pct(v.hi_pct)}", "${pct(v.hi_pct)} ~ ${pct(v.lo_pct)}"),
    ("알림을 숨김", "app.js", 'flag.hidden = v.display === "없음" || !v.n;', 'flag.hidden = v.display !== "표본 문구" || !v.n;'),
    ("n 자리에 맞은 수", "app.js", "`지난 ${v.n}번의", "`지난 ${v.hits}번의"),
    ("단위를 늘 원으로", "app.js", '`${v.currency === "KRW" ? "원" : "달러"} 가격', "`원 가격"),
    ("적중률을 화면에서 다시 반올림", "app.js", "num(v.hit_rate_pct, 1)", "num(Math.round(v.hit_rate_pct), 1)"),
    ("참고 문구를 뺌", "app.js", " 데이터가 2년뿐이라 참고로만 보는 값입니다.`", "`"),
    ("알림 방향을 뒤바꿈", "app.js", '(v.side === "넓음"', '(v.side !== "넓음"'),
    ("원화 아님 문구를 뺌", "index.html", " 환율과 분배금은 넣지 않아 원화 범위가 아닙니다.", ""),
    ("지난 창 0개에도 표본 칸을 보임", "app.js", 'flag.hidden = v.display === "없음" || !v.n;', 'flag.hidden = v.display === "없음";'),
]


def leaky_pick(days, asof):
    """서버 쪽 누수 심기용: 거래일은 그대로, 쉬는 날은 다음 거래일을 고름(bisect_right 자리에 넣음)."""
    from bisect import bisect_left
    return min(bisect_left(days, asof) + 1, len(days))


def selftest():
    res = {}
    # 서버: 쉬는 날에 다음 거래일을 고르는 누수를 모든 날짜 대조가 잡는지
    old_pick = screen_server.bisect_right
    screen_server.bisect_right = leaky_pick
    httpd, _ = serve(screen_data.eval_db(), 0, None)
    try:
        swept = sweep(httpd.app)
    finally:
        stop(httpd)
        screen_server.bisect_right = old_pick
    res["서버가 쉬는 날에 다음 거래일을 고름"] = bool(swept["bad"])
    print(f"  {'잡음' if swept['bad'] else '못 잡음'}  서버가 쉬는 날에 다음 거래일을 고름(다른 날 {len(swept['bad'])}개)")

    rows = json.loads(EXPECTED.read_text(encoding="utf-8"))["rows"]
    pick = []
    for test in (lambda r: judge(r["n"], r["hits"])[1] == "넓음", lambda r: judge(r["n"], r["hits"])[1] == "좁음",
                 lambda r: r["display"] == "없음" and r["market"] != "KRX",
                 lambda r: r["display"] == "표본 문구", lambda r: r["market"] == "KRX"):
        pick.append(next(r for r in rows if test(r) and r not in pick))
    pick += dev_rows()[1:]  # 지난 창 0개
    old = screen_server.STATIC
    for name, fname, a, b in BUGS:
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(ROOT / "screen", Path(d) / "screen")
            f = Path(d) / "screen" / fname
            src = f.read_text(encoding="utf-8")
            assert src.count(a) == 1, f"심지 못함({name}): {a!r}"
            f.write_text(src.replace(a, b), encoding="utf-8")
            screen_server.STATIC = (Path(d) / "screen").resolve()
            httpd, _ = serve(screen_data.eval_db(), 0, None)
            try:
                got = screen_pass(httpd.server_address[1], pick)
            finally:
                stop(httpd)
                screen_server.STATIC = old
        res[name] = any(got.values())
        print(f"  {'잡음' if res[name] else '못 잡음'}  {name}")
    ok = all(res.values())
    print(f"자체 시험 {sum(res.values())}/{len(res)} 잡음(화면 행 {len(pick)}개: " + ", ".join(f"{r['symbol']} {r['base']}" for r in pick)
          + ") → " + ("통과" if ok else "실패"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="F 화면 8번 칸(앞으로의 범위) 대조")
    ap.add_argument("--selftest", action="store_true", help="서버와 화면 사본에 잘못을 심어 대조가 잡는지")
    sys.exit(selftest() if ap.parse_args().selftest else main())
