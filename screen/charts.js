// 3번 칸(가격과 내 매매)과 4번 칸(누적 손익)의 SVG 그림. 값은 API 응답을 그대로 쓰고, 읽기 줄(readout)에도
// API 값을 그대로 보입니다. 마우스, 손가락, 키보드(왼쪽과 오른쪽 화살표, Home, End)로 날짜를 옮길 수 있습니다.
import { krw, krwShort, price, priceShort } from "./format.js";

const NS = "http://www.w3.org/2000/svg";
const M = { l: 58, r: 14, t: 16, b: 30 };
// 그림 크기는 그릴 때마다 칸의 실제 폭에 맞춥니다(휴대폰에서 글자가 작아지지 않게). 사건 처리기는 그린 때의 크기를 씁니다.
let W = 560, H = 280;

function fit(svg) {
  const w = Math.round(svg.getBoundingClientRect().width) || 560;
  W = Math.max(300, Math.min(760, w));
  H = Math.round(Math.max(220, Math.min(300, W * 0.52)));
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
}

function el(tag, attrs, parent, text) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

function niceTicks(lo, hi, n = 4) {
  if (hi === lo) { hi = lo + 1; lo = lo - 1; }
  const raw = (hi - lo) / n;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

function monthTicks(dates) {
  const firsts = [];
  let prev = "";
  dates.forEach((d, i) => {
    const m = d.slice(0, 7);
    if (m !== prev) { if (i > 0) firsts.push(i); prev = m; }
  });
  const every = Math.max(1, Math.ceil(firsts.length / Math.max(3, Math.floor(W / 90))));
  return firsts.filter((_, k) => k % every === 0);
}

// strip을 주면 그림 아래에 그 높이만큼 띠를 비워 둡니다(가격 그림의 큰 움직임 눈금 자리)
function frame(svg, dates, lo, hi, yfmt, strip = 0) {
  svg.replaceChildren();
  const w = W, h = H;
  const x = (i) => M.l + (dates.length < 2 ? 0 : (i * (w - M.l - M.r)) / (dates.length - 1));
  const y = (v) => M.t + ((hi - v) * (h - M.t - M.b - strip)) / (hi - lo || 1);
  const g = el("g", {}, svg);
  for (const t of niceTicks(lo, hi)) {
    if (t < lo || t > hi) continue;
    el("line", { class: "ax-line", x1: M.l, x2: W - M.r, y1: y(t), y2: y(t) }, g);
    el("text", { class: "ax", x: M.l - 6, y: y(t) + 4, "text-anchor": "end" }, g, yfmt(t));
  }
  for (const i of monthTicks(dates)) {
    const d = dates[i];
    el("line", { class: "ax-line", x1: x(i), x2: x(i), y1: H - M.b, y2: H - M.b + 4 }, g);
    el("text", { class: "ax", x: x(i), y: H - M.b + 17, "text-anchor": "middle" }, g, `${d.slice(2, 4)}.${d.slice(5, 7)}`);
  }
  return { x, y };
}

function pathOf(points) {
  let d = "", pen = false;
  for (const p of points) {
    if (!p) { pen = false; continue; }
    d += (pen ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1);
    pen = true;
  }
  return d;
}

// 마우스, 손가락, 키보드로 날짜를 고르는 공통 부분
function interact(svg, n, x, onPick, initial, onActivate) {
  const w = W;
  const cross = el("line", { class: "cross", y1: M.t, y2: H - M.b, visibility: "hidden" }, svg);
  let cur = initial;
  const pick = (i, show = true) => {
    cur = Math.max(0, Math.min(n - 1, i));
    cross.setAttribute("x1", x(cur)); cross.setAttribute("x2", x(cur));
    cross.setAttribute("visibility", show ? "visible" : "hidden");
    onPick(cur);
  };
  const fromEvent = (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * w;
    return Math.round(((px - M.l) / (w - M.l - M.r)) * (n - 1));
  };
  svg.onpointermove = (ev) => pick(fromEvent(ev));
  svg.onpointerdown = (ev) => pick(fromEvent(ev));
  svg.onpointerleave = () => pick(initial, false);
  svg.onclick = () => onActivate?.(cur);
  svg.onkeydown = (ev) => {
    if (ev.key === "Enter" && onActivate) { ev.preventDefault(); onActivate(cur); return; }
    const step = ev.shiftKey ? 20 : 1;
    const k = { ArrowLeft: cur - step, ArrowRight: cur + step, Home: 0, End: n - 1 }[ev.key];
    if (k === undefined) return;
    ev.preventDefault();
    pick(k);
  };
  svg.onblur = () => pick(initial, false);
  pick(initial, false);
}

// ---------- 3번 칸: 가격과 내 매매 ----------

// onBig(날짜): 큰 움직임 눈금 근처(6px 안)를 누르거나 그 날에서 Enter를 누르면 7번 칸을 엶
export function drawPrice(svg, readout, res, rowNo, onBig) {
  fit(svg);
  const cur = res.currency;
  const all = res.prices;
  const end = res.asof_price_date;
  let endIdx = all.findIndex(([d]) => d === end);
  if (endIdx < 0) endIdx = all.length - 1;
  const live = (res.steps || []).filter((s) => !s.excluded);
  // 창은 계산에서 빠진 줄(빈 원)까지 담습니다. 기준일 뒤 매매는 창 밖이라 그리지 않습니다
  const first = [...(res.steps || []).map((s) => s.date), ...(res.holding ? [res.holding[0]] : [])].sort()[0];
  const startIdx = first ? Math.max(0, all.findIndex(([d]) => d >= first) - 15) : Math.max(0, endIdx - 250);
  const pts = all.slice(startIdx, endIdx + 1);
  const dates = pts.map(([d]) => d);
  const pos = new Map(dates.map((d, i) => [d, i]));
  // 휴장일에 넣은 매매는 그 날짜가 시세에 없어, 계산에 쓴 직전 거래일 자리에 표시합니다
  const at = (d) => {
    if (pos.has(d)) return pos.get(d);
    if (d > dates[dates.length - 1]) return -1;
    let i = -1;
    for (let j = 0; j < dates.length && dates[j] <= d; j++) i = j;
    return i;
  };

  // 평균 단가: 장부에 들어간 줄마다 그 뒤 평균 단가, 남은 수량이 0이면 끊음
  const avg = new Array(dates.length).fill(null);
  const sorted = [...live].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  let k = 0, a = null;
  dates.forEach((d, i) => {
    while (k < sorted.length && sorted[k].date <= d) { a = sorted[k].qty_after > 0 ? sorted[k].avg_cost_after : null; k++; }
    avg[i] = a;
  });

  const vals = [...pts.map(([, c]) => c), ...avg.filter((v) => v !== null),
    ...(res.steps || []).filter((s) => s.price !== null && at(s.date) >= 0).map((s) => s.price)];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.08 || hi * 0.05 || 1;
  lo -= pad; hi += pad;
  // 큰 움직임은 가격 선 위가 아니라 그림 아래 띠에 눈금으로 그립니다(30개 넘게 몰리면 선을 덮음)
  const STRIP = 12;
  const { x, y } = frame(svg, dates, lo, hi, (t) => priceShort(t, cur), STRIP);

  const bm = res.big_moves;
  if (bm && bm.not_judged_in_holding && res.holding) {
    const s = Math.max(0, at(res.holding[0]));
    const e = bm.judged_from && pos.has(bm.judged_from) ? pos.get(bm.judged_from) : dates.length - 1;
    if (e > s) el("rect", { class: "band", x: x(s), y: M.t, width: x(e) - x(s), height: H - M.t - M.b }, svg);
  }
  el("path", { class: "l-avg", d: pathOf(avg.map((v, i) => (v === null ? null : [x(i), y(v)]))) }, svg);
  el("path", { class: "l-main", d: pathOf(pts.map(([, c], i) => [x(i), y(c)])) }, svg);
  for (const d of bm ? bm.dates : []) {
    if (pos.has(d)) el("line", { class: "m-big", "data-d": d, x1: x(pos.get(d)), x2: x(pos.get(d)), y1: H - M.b - STRIP + 2, y2: H - M.b - 1 }, svg);
  }
  for (const s of res.steps || []) {
    const i = at(s.date);
    if (i < 0) continue;
    const cx = x(i), cy = y(s.price);
    if (s.excluded) { el("circle", { class: "m-out", cx, cy, r: 5 }, svg); continue; }
    const tri = s.side === "BUY"
      ? `M${cx},${cy + 5} l6,10 h-12 z`
      : `M${cx},${cy - 5} l6,-10 h-12 z`;
    el("path", { class: s.side === "BUY" ? "m-buy" : "m-sell", d: tri }, svg);
  }
  const dot = el("circle", { class: "dot-now", r: 3.6, visibility: "hidden" }, svg);

  const stepAt = new Map();
  for (const s of res.steps || []) { const i = at(s.date); stepAt.set(i, [...(stepAt.get(i) || []), s]); }
  const bigSet = new Set(bm ? bm.dates : []);
  const show = (i) => {
    const [d, c] = pts[i];
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(c)); dot.setAttribute("visibility", "visible");
    const parts = [`<b>${d}</b>`, `종가 <b>${price(c, cur)}</b>`];
    if (avg[i] !== null) parts.push(`평균 단가 <b>${price(avg[i], cur)}</b>`);
    for (const s of stepAt.get(i) || []) {
      const when = s.date !== d ? `(${s.date} 휴장일 매매)` : "";
      parts.push(`${rowNo(s.trade_index)}번 줄 ${s.side === "BUY" ? "매수" : "매도"} ${s.qty}주 @ ${price(s.price, cur)}${when}${s.excluded ? "(계산에서 빠짐)" : ""}`);
    }
    if (bigSet.has(d)) parts.push(onBig ? "큰 움직임(누르거나 Enter로 「그때 무슨 일이」)" : "큰 움직임");
    readout.innerHTML = parts.join(", ");
  };
  // 누른 자리에서 6px 안의 가장 가까운 큰 움직임 날. 하루가 1px 안팎이라 눈금을 정확히 누르기 어려워서
  const bigIdx = dates.map((d, j) => (bigSet.has(d) ? j : -1)).filter((j) => j >= 0);
  const nearBig = (i) => {
    let best = null, gap = 6;
    for (const j of bigIdx) { const g = Math.abs(x(j) - x(i)); if (g <= gap) { gap = g; best = dates[j]; } }
    return best;
  };
  interact(svg, pts.length, x, show, pts.length - 1, onBig ? (i) => { const d = nearBig(i); if (d) onBig(d); } : undefined);

  const buys = live.filter((s) => s.side === "BUY").length, sells = live.filter((s) => s.side === "SELL").length;
  const label = `${res.name} ${dates[0]} ~ ${dates[dates.length - 1]} 종가 흐름. 매수 ${buys}번, 매도 ${sells}번, 보유 기간의 큰 움직임 ${bm ? bm.dates.length : 0}일.`;
  svg.setAttribute("aria-label", label);
  return label;
}

// ---------- 4번 칸: 누적 손익 ----------

export function drawPnl(svg, readout, res) {
  fit(svg);
  const daily = res.daily || [];
  svg.replaceChildren();
  if (!daily.length) {
    readout.textContent = "";
    el("text", { class: "ax", x: W / 2, y: H / 2, "text-anchor": "middle" }, svg, "장부에 들어간 매매가 없어 그릴 선이 없습니다");
    svg.setAttribute("aria-label", "누적 손익 선 없음");
    return "";
  }
  const dates = daily.map((r) => r.date);
  const vals = daily.flatMap((r) => [r.pnl_krw, r.bench_krw]).concat([0]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad; hi += pad;
  const { x, y } = frame(svg, dates, lo, hi, krwShort);

  // 최대 낙폭 구간: 고점에서 뺀 값이 가장 작은 날과 그 고점이 처음 생긴 날
  let trough = -1, worst = 0;
  daily.forEach((r, i) => { const g = r.pnl_krw - r.peak_krw; if (g < worst) { worst = g; trough = i; } });
  if (trough > 0 && res.summary && res.summary.max_drawdown_krw !== 0) {
    let peak = trough;
    while (peak > 0 && daily[peak].pnl_krw !== daily[trough].peak_krw) peak--;
    el("rect", { class: "dd", x: x(peak), y: M.t, width: Math.max(1, x(trough) - x(peak)), height: H - M.t - M.b }, svg);
    const mdd = res.summary.max_drawdown_krw;
    const tx = Math.min(Math.max((x(peak) + x(trough)) / 2, M.l + 70), W - M.r - 70);
    el("text", { class: "lbl", x: tx, y: H - M.b - 8, "text-anchor": "middle" }, svg, `최대 낙폭 ${krw(mdd)}`);
  }
  el("line", { class: "zero-line", x1: M.l, x2: W - M.r, y1: y(0), y2: y(0) }, svg);
  el("path", { class: "l-bench", d: pathOf(daily.map((r, i) => [x(i), y(r.bench_krw)])) }, svg);
  el("path", { class: "l-main", d: pathOf(daily.map((r, i) => [x(i), y(r.pnl_krw)])) }, svg);
  const dot = el("circle", { class: "dot-now", r: 3.6, visibility: "hidden" }, svg);
  const dot2 = el("circle", { class: "dot-now", r: 3, visibility: "hidden" }, svg);

  const show = (i) => {
    const r = daily[i];
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(r.pnl_krw)); dot.setAttribute("visibility", "visible");
    dot2.setAttribute("cx", x(i)); dot2.setAttribute("cy", y(r.bench_krw)); dot2.setAttribute("visibility", "visible");
    readout.innerHTML = `<b>${r.date}</b>, 내 매매 <b>${krw(r.pnl_krw, true)}</b>, SPY에 넣었다면 <b>${krw(r.bench_krw, true)}</b>`;
  };
  interact(svg, daily.length, x, show, daily.length - 1);
  const last = daily[daily.length - 1];
  const label = `누적 손익 ${dates[0]} ~ ${last.date}. 마지막 날 내 매매 ${krw(last.pnl_krw, true)}, 같은 돈을 SPY에 넣었다면 ${krw(last.bench_krw, true)}.`;
  svg.setAttribute("aria-label", label);
  return label;
}

