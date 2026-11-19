"""증권사별 계좌와 「지금 잔고」 줄(2026-11-19 중간 판 의견, docs/ai/F-screen.md 7절 「증권사와 지금 잔고」).

손익 SQL(sql/pnl/)은 그대로 둡니다. 이 파일은 매매 줄을 증권사(계좌)로 나누고, 「지금 잔고」 줄을 데이터 첫날의 보유로
바꿔 손익 SQL에 넣는 일만 합니다.

- 증권사: 증권사 앱은 계좌마다 따로 이동평균 평단을 냅니다. 그래서 증권사마다 손익 SQL을 따로 돌려 평단, 실현과 평가,
  매도별 손익, 남은 주식 수익률을 내고, 모든 매매를 합쳐 한 번 더 돌려 날마다의 누적 손익, SPY 선, 연 환산 수익률을 냅니다.
  손익 합계(판 돈 + 남은 주식 평가액 - 산 돈)는 평단을 어떻게 세든 같으므로, 증권사별 합계와 합친 합계가 같은지 시험이
  봅니다(eval/f_screen/check_api.py). 증권사를 고르지 않은 줄은 한 계좌(빈 이름)로 봅니다.
- 지금 잔고(side HOLD): 증권사 앱 잔고 화면의 수량, 평균 단가, (미국 종목) 평균 매입 환율입니다. 같은 증권사의 데이터 기간
  안 매매를 거꾸로 되짚어 데이터 첫날의 보유(수량, 현지 통화 원가, 원화 원가)를 셈하고, 그 원가로 첫날 산 줄(시작 보유)을
  장부 맨 앞에 넣습니다. 데이터 기간 전 매매는 지금처럼 OUT_OF_DATA_RANGE로 빠지고, 그 몫은 시작 보유가 대신합니다.
  거꾸로 셈하기는 손익 SQL의 장부와 같은 규칙입니다: 매수는 수량과 원가를 더하고, 매도는 원가를 같은 비율로 덜어 냅니다.
  거꾸로 갈 때 매도는 원가를 (팔기 전 수량 / 판 뒤 수량)배로 되돌립니다. 판 뒤 수량이 0이면 그 앞의 평단을 알 수 없습니다.
- 시작 보유가 있으면 SPY 비교와 연 환산 수익률은 세지 않습니다(그 주식을 언제 얼마의 돈으로 샀는지 모름).
"""
from decimal import ROUND_HALF_UP, Decimal

from ai.pnl import compute

BROKERS = [
    ("TOSS", "토스증권"), ("KAKAOPAY", "카카오페이증권"), ("KIS", "한국투자증권"), ("KIWOOM", "키움증권"),
    ("MIRAE", "미래에셋증권"), ("SAMSUNG", "삼성증권"), ("NH", "NH투자증권"), ("KB", "KB증권"),
    ("SHINHAN", "신한투자증권"), ("DAISHIN", "대신증권"), ("HANA", "하나증권"), ("MERITZ", "메리츠증권"),
    ("ETC", "그 밖의 증권사"),
]
BROKER_NAME = dict(BROKERS)
NONE_NAME = "증권사 안 고름"

# 손익 SQL의 경고(eval/f_screen/warnings.json, 잠긴 파일)와 따로 둔 안내. 줄을 빼지 않고 어떻게 셌는지만 알립니다.
NOTES = {
    "HOLD_USED": "지금 잔고를 데이터 첫날({first})의 보유 {qty}주, 평균 단가 {price}로 바꿔 셌어요(기간 안 매매를 거꾸로 되짚음).",
    "HOLD_EXPLAINED": "지금 잔고가 기간 안 매매로 모두 설명돼 데이터 첫날 전 보유는 없어요.",
    "PRE_WINDOW_COVERED": "데이터 기간 전 줄은 계산에 쓰지 않고, 같은 증권사의 지금 잔고 줄이 대신해요.",
    "PRE_WINDOW_NEEDS_HOLD": "데이터 기간({first}부터) 전 매매가 있어 그 몫을 셀 수 없어요. 그 증권사 앱 잔고 화면의 수량, 평균 단가{fx}를 「지금 잔고」 줄로 넣으면 기간 전 보유까지 셀 수 있어요.",
    "HOLD_TOO_FEW": "지금 잔고 수량이 기간 안에서 산 수량보다 적어 거꾸로 셀 수 없어요. 지금 잔고나 매매 줄의 증권사를 확인하세요.",
    "HOLD_SOLD_OUT": "기간 안에 한 번 모두 판 적이 있어, 그 앞의 평균 단가를 지금 잔고로 알 수 없어요. 지금 잔고 줄을 계산에서 뺐어요.",
    "HOLD_COST_MISMATCH": "지금 잔고의 평균 단가로 되짚은 원가가 기간 안 매매로 센 원가와 {gap}만큼 달라요(수수료가 들어갔거나 증권사가 평단을 다르게 셀 수 있어요). 기간 안 매매로 센 값을 썼어요.",
    "HOLD_BAD_COST": "지금 잔고의 평균 단가로 되짚은 데이터 첫날 원가가 0 이하라 거꾸로 셀 수 없어요. 지금 잔고 줄을 계산에서 뺐어요.",
    "BENCH_OFF": "데이터 첫날 전부터 가진 주식이 있어 SPY 비교와 연 환산 수익률은 세지 않아요(그 주식을 언제 얼마의 돈으로 샀는지 몰라요).",
}
# 증권사별 합으로 바꿔 쓰는 요약 칸(평단에 기대는 값). 나머지(SPY, 낙폭, 연 환산 등)는 합친 매매의 값입니다.
SUMMED = ("realized_local", "unrealized_local", "total_local", "realized_krw", "unrealized_krw", "total_krw",
          "invested_krw", "price_effect_krw", "fx_effect_krw", "cross_effect_krw")
P4 = Decimal("0.0001")


def broker_name(code):
    return BROKER_NAME.get(code, NONE_NAME) if code else NONE_NAME


def _note(code, broker, index=None, **fill):
    return {"code": code, "broker": broker, "broker_name": broker_name(broker), "trade_index": index,
            "message": NOTES[code].format(**fill)}


def _groups(trades):
    """증권사 순서는 처음 나온 줄의 순서. 값: (지금 잔고 줄 번호 또는 None, [(줄 번호, 줄)])"""
    out = {}
    for k, t in enumerate(trades):
        g = out.setdefault(t.get("broker") or "", [None, []])
        if t["side"] == "HOLD":
            g[0] = k
        else:
            g[1].append((k, t))
    return out


def _fmt(v, currency):
    return f"{v:,.0f}원" if currency == "KRW" else f"{v:,.4f}".rstrip("0").rstrip(".") + "달러"


def opening(engine, world, symbol, rows, hold, bench):
    """데이터 첫날의 보유를 거꾸로 셈합니다. rows: 같은 증권사의 매매 [(줄 번호, 줄)], hold: 지금 잔고 줄.
    돌려주는 것: (시작 보유 줄 또는 None, 안내 목록, 되짚은 값)"""
    sym = world["symbols"][symbol]
    days = sorted(sym["rows"])
    first, last = days[0], days[-1]
    usd = sym["currency"] == "USD"
    inside = [t for _, t in rows if first <= t["d"] <= last]
    q, cl = Decimal(hold["q"]), Decimal(hold["q"]) * Decimal(str(hold["p"]))
    ck = cl * (Decimal(str(hold["fx"])) if usd else 1)
    steps = []
    if inside:
        # 그날 종가나 매매기준율로 채운 값까지 손익 SQL과 같게 쓰려고, 기간 안 매매만 데이터 끝날 기준으로 한 번 돌려
        # 줄마다 계산에 쓴 가격과 환율을 받습니다(시작 보유가 없어 초과 매도로 표시돼도 가격과 환율은 나옴).
        _, _, _, kept = compute(engine, world, symbol, inside, last, bench)
        if kept:
            _, steps = engine.series()
    # 거꾸로 되짚기. 보유가 0이 되는 매수(되짚어 0)를 지나면 그 앞 원가는 0입니다. 모두 판 매도(판 뒤 0)를 지나면 그 앞
    # 원가는 지금 잔고로 알 수 없어(known False), 그 앞 매수로 보유가 다시 0이 되면 알게 됩니다.
    known, gap, most = True, None, q
    for _, cd, side, qty, px, fx, *_ in sorted(steps, key=lambda s: (s[1], s[0]), reverse=True):
        qty, px, fx = Decimal(qty), Decimal(str(px)), Decimal(str(fx))
        if side == "BUY":
            q -= qty
            if known:
                cl, ck = cl - qty * px, ck - qty * px * fx
            if q < 0:
                return None, ["HOLD_TOO_FEW"], None
            if q == 0:
                # 평단 차이(수수료, 앱의 반올림 등): 한 주에 0.01(현지 통화)보다 크면 알림
                if known and gap is None and abs(cl) > Decimal("0.01") * most:
                    gap = cl
                cl, ck, known = Decimal(0), Decimal(0), True
        else:
            if q == 0:
                known = False
            elif known:
                cl, ck = cl * (q + qty) / q, ck * (q + qty) / q
            q += qty
            most = max(most, q)
    back = {"qty": int(q), "cost_local": cl if known else None, "cost_krw": ck if known else None, "gap": gap}
    if q == 0:
        # 지금 잔고가 기간 안 매매로 모두 설명됨
        return None, (["HOLD_EXPLAINED"] if gap is None else ["HOLD_EXPLAINED", "HOLD_COST_MISMATCH"]), back
    if not known:
        return None, ["HOLD_SOLD_OUT"], back
    if cl <= 0 or (usd and ck <= 0):
        return None, ["HOLD_BAD_COST"], back
    row = {"d": first, "side": "BUY", "q": int(q), "p": (cl / q).quantize(P4, ROUND_HALF_UP), "opening": True}
    if usd:
        row["fx"] = (ck / cl).quantize(P4, ROUND_HALF_UP)
    return row, (["HOLD_USED"] if gap is None else ["HOLD_USED", "HOLD_COST_MISMATCH"]), back


def plan(engine, world, symbol, trades, bench):
    """증권사마다 손익 SQL에 넣을 줄. 돌려주는 것: [{broker, hold, rows: [(줄 번호, 줄)], open, notes}]"""
    sym = world["symbols"][symbol]
    first = min(sym["rows"])
    out = []
    for broker, (hold, rows) in _groups(trades).items():
        g = {"broker": broker, "hold": hold, "rows": rows, "open": None, "notes": [], "back": None}
        if hold is not None:
            row, codes, back = opening(engine, world, symbol, rows, trades[hold], bench)
            g["open"], g["back"] = row, back
            for c in codes:
                fill = {}
                if c == "HOLD_USED":
                    fill = {"first": first, "qty": f"{row['q']:,}", "price": _fmt(row["p"], sym["currency"])}
                elif c == "HOLD_COST_MISMATCH":
                    fill = {"gap": _fmt(abs(back["gap"]), sym["currency"])}
                g["notes"].append(_note(c, broker, hold, **fill))
            pre = [k for k, t in rows if t["d"] < first]
            if pre and row is not None:
                g["notes"].append(_note("PRE_WINDOW_COVERED", broker, pre[0]))
        else:
            # 기간 전 매매만 있고 지금 잔고 줄이 없으면 그 증권사의 첫 기간 전 줄에 길을 알림(11/19 중간 판: 장기 보유가 비어
            # 보였는데 화면에 지금 잔고 줄을 넣으라는 안내가 없었음). 화면은 이 안내 옆에 「지금 잔고 줄 넣기」 단추를 둠
            pre = [k for k, t in rows if t["d"] < first]
            if pre:
                fx = ", 평균 매입 환율" if sym["currency"] == "USD" else ""
                g["notes"].append(_note("PRE_WINDOW_NEEDS_HOLD", broker, pre[0], first=first, fx=fx))
        out.append(g)
    return out


def effective(engine, world, symbol, trades, bench):
    """묻고 답하기(9번 칸)에 넘길 줄: 증권사를 가리지 않고 시작 보유와 매매를 합친 손익 SQL 입력(지금 잔고 줄은 뺌)."""
    first = min(world["symbols"][symbol]["rows"])
    opens, rest = [], []
    for g in plan(engine, world, symbol, trades, bench):
        if g["open"]:
            opens.append({k: v for k, v in g["open"].items() if k != "opening"})
        rest += [(k, t) for k, t in g["rows"] if not (g["open"] and t["d"] < first)]
    return opens + [{k: v for k, v in t.items() if k != "broker"} for _, t in sorted(rest, key=lambda r: r[0])]


def run(engine, world, symbol, trades, asof, bench):
    """화면 손익. trades의 줄 번호가 경고와 단계의 trade_index입니다.

    돌려주는 것(dict): summary, sells, warnings, steps(증권사별 장부의 단계, 줄 번호와 증권사를 붙임),
    combined(합친 매매의 (kept 줄 번호, series)), accounts, notes, opening(시작 보유가 있는지), multi(증권사가 둘 이상인지)
    """
    groups = plan(engine, world, symbol, trades, bench)
    simple = len(groups) == 1 and groups[0]["hold"] is None
    per, warnings, steps, sells, accounts, dropped = [], [], [], [], [], set()
    for g in groups:
        # 증권사 한 곳의 장부: 시작 보유(있으면)를 맨 앞에. 줄 번호는 시작 보유를 지금 잔고 줄에 맞춤
        rows = ([(g["hold"], g["open"])] if g["open"] else []) + g["rows"]
        if not rows:
            per.append((g, None, None))
            continue
        summary, gs, gw, kept = compute(engine, world, symbol, [t for _, t in rows], asof, bench)
        idx = [rows[k][0] for k in kept]
        gseries = engine.series() if kept else ([], [])
        gsteps = gseries[1]
        is_open = {i for i, (k, t) in enumerate(rows) if t.get("opening")}
        for w in gw:
            if w["trade_index"] in is_open and w["code"] == "PRICE_OUT_OF_RANGE":
                continue  # 시작 보유의 가격은 평단이라 그날 가격 범위와 견주지 않음
            k = rows[w["trade_index"]][0]
            warnings.append({"code": w["code"], "trade_index": k})
            if w["code"] in ("OVERSOLD", "AFTER_ASOF", "BAD_QTY"):
                dropped.add(k)
        for seq, cd, side, q, p, fx, over, qa, cost in gsteps:
            pos = kept[seq]
            steps.append({"trade_index": idx[seq], "broker": g["broker"], "opening": pos in is_open,
                          "row": (seq, cd, side, q, p, fx, over, qa, cost)})
        for s in gs:
            sells.append({**s, "broker": g["broker"]})
        per.append((g, summary, (idx, gseries)))
    # 합친 매매: 증권사별 장부에서 빠진 줄(초과 매도 등)은 여기서도 빼서 두 길의 합계가 같은 줄로 셈
    combined_rows = []
    for g in groups:
        if g["open"]:
            combined_rows.append((g["hold"], g["open"]))
        combined_rows += [(k, t) for k, t in g["rows"] if k not in dropped]
    combined_rows.sort(key=lambda r: (not r[1].get("opening"), r[0]))
    if simple:
        # 증권사 하나, 지금 잔고 없음: 전과 같은 한 번의 계산(합친 매매가 곧 그 증권사의 매매)
        summary = per[0][1] or {}
        combined_idx, series = per[0][2] or ([], ([], []))
    else:
        summary, _, _, kept = compute(engine, world, symbol, [t for _, t in combined_rows], asof, bench) \
            if combined_rows else ({}, [], [], [])
        series = engine.series() if kept else ([], [])
        combined_idx = [combined_rows[k][0] for k in kept]
    for g, s, _ in per:
        accounts.append(_account(g, s, world["symbols"][symbol]["currency"]))
    has_open = any(g["open"] for g in groups)
    notes = [n for g in groups for n in g["notes"]]
    if has_open:
        notes.append(_note("BENCH_OFF", None))
    if summary and len(groups) > 1:
        parts = [s for _, s, _ in per if s]
        sums = {c: sum(s[c] for s in parts) for c in SUMMED}
        # 넣은 원화도 계좌 합(계좌마다 원 단위로 반올림한 값이라 합친 계산과 1원 다를 수 있음), 단순 수익률은 그 합으로 다시 셈
        # (손익 SQL pnl_position과 같이 소수 여섯째 자리로 먼저 맞춘 뒤 둘째 자리)
        inv = Decimal(str(sums["invested_krw"]))
        simple = None if not inv else (Decimal(str(sums["total_krw"])) / inv * 100).quantize(
            Decimal("0.000001"), ROUND_HALF_UP).quantize(Decimal("0.01"), ROUND_HALF_UP)
        summary = {**summary, **sums, "simple_return_pct": simple, "combined_total_krw": summary["total_krw"],
                   "combined_invested_krw": summary["invested_krw"], "avg_cost_local": None, "avg_buy_fx": None}
    warnings.sort(key=lambda w: (w["trade_index"], w["code"]))
    steps.sort(key=lambda s: (s["row"][1], s["trade_index"] if not s["opening"] else -1))
    sells.sort(key=lambda s: (s["date"],))
    return {"summary": summary, "sells": sells, "warnings": warnings, "steps": steps,
            "combined": (combined_idx, series), "accounts": accounts, "notes": notes,
            "opening": has_open, "multi": len(groups) > 1}


def holding_return(s, currency):
    """증권사 앱의 「수익률」: 남은 주식의 평가 손익 / 남은 주식의 매입 금액(현지 통화). 미국 종목은 원화 기준도."""
    if not s or not s.get("qty_left") or not s.get("avg_cost_local"):
        return None, None
    basis = Decimal(str(s["qty_left"])) * Decimal(str(s["avg_cost_local"]))
    local = float(Decimal(str(s["unrealized_local"])) / basis * 100) if basis else None
    krw = None
    if currency == "USD" and s.get("avg_buy_fx"):
        kb = basis * Decimal(str(s["avg_buy_fx"]))
        krw = float(Decimal(str(s["unrealized_krw"])) / kb * 100) if kb else None
    return (None if local is None else round(local, 2)), (None if krw is None else round(krw, 2))


def _account(g, s, currency):
    local, krw = holding_return(s, currency)
    a = {"broker": g["broker"], "broker_name": broker_name(g["broker"]), "hold_index": g["hold"],
         "opening": None, "summary": None, "holding_return_pct": local, "holding_return_krw_pct": krw}
    if g["open"]:
        a["opening"] = {"date": g["open"]["d"], "qty": g["open"]["q"], "avg_cost_local": g["open"]["p"],
                        "avg_buy_fx": g["open"].get("fx")}
    if s:
        # 손익 SQL 요약 칸 전부(레이크 대조가 Gold 손익 마트의 계좌별 행과 칸마다 견줌, loaders/screen_trades.py --verify)
        a["summary"] = {k: v for k, v in s.items() if not k.startswith("gap_")}
    return a
