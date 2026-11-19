// 내 투자 돌아보기(docs/ai/F-screen.md 2절). 계산은 모두 서버(ai/screen_server.py)가 하고, 이 파일은 입력을 모아 보내고
// 돌려받은 값을 그대로 보입니다. 숫자를 다시 계산하거나 반올림하지 않습니다(그림의 위치와 축 눈금만 셈).
import { drawPnl, drawPrice } from "./charts.js";
import { krw, local, num, pct, price, tone } from "./format.js";

// 큰 금액 줄이기(3판 뼈대의 숫자 줄이기 규칙, 담당자 「가독성이 중요」). 100만 이상은 만, 1억 이상은 억(100억 이상은 정수),
// 1조 이상은 조. 반올림은 반올림 절반 올림. 줄인 값은 보이는 글에만 쓰고, 정확한 값(API 값 그대로)은 바로 아래 작은 줄과 title에 둡니다.
const MINUS = "−";
function half(x, n) { const f = 10 ** n; return Math.round((x * f) + Number.EPSILON * f) / f; }
function one(x) { const v = half(x, 1); return Number.isInteger(v) ? String(v) : v.toFixed(1); }
const grp = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
function krwBig(v, signed = false) {
  if (v === null || v === undefined) return "–";
  const a = Math.abs(v), sign = v < 0 ? MINUS : signed && v > 0 ? "+" : "";
  if (a >= 1e12) return sign + one(a / 1e12) + "조원";
  if (a >= 1e8) { const q = a / 1e8; return sign + (q >= 100 ? grp(Math.round(q)) : one(q)) + "억원"; }
  if (a >= 1e6) return sign + grp(Math.round(a / 1e4)) + "만원";
  return krw(v, signed);
}
const shortened = (v) => v !== null && v !== undefined && Math.abs(v) >= 1e6;

const $ = (id) => document.getElementById(id);
const DEFAULT_SYMBOL = "QQQ";
const EXAMPLE = [
  { date: "2025-03-03", side: "BUY", qty: "10" },
  { date: "2025-08-01", side: "BUY", qty: "5" },
  { date: "2026-02-02", side: "SELL", qty: "8" },
];
const NUM = /^\d+(\.\d+)?$/;
const PRICE = /^\d+(\.\d{1,4})?$/;

const st = {
  symbols: new Map(), calendar: null, sym: null, asof: null, asofErr: null, rows: [], saved: { symbols: {}, last: null },
  saveOn: false, example: false, seq: 0, ctl: null, timer: null, saveTimer: null, res: null, map: [], savedAt: null,
  saveErr: null, notes: null, rseq: 0, rctl: null, asking: false, eseq: 0, evBusy: false, evSel: null, brokers: [], pseq: 0,
};
const brokerName = (code) => st.brokers.find((b) => b.code === code)?.name || "증권사 안 고름";
const isHold = (r) => (r.side || "BUY") === "HOLD";

// ---------- 서버 ----------

async function api(path, init = {}) {
  const r = await fetch(path, { ...init, headers: init.body ? { "Content-Type": "application/json" } : {} });
  let body = null;
  try { body = await r.json(); } catch { /* 본문 없음 */ }
  return { status: r.status, body };
}

// ---------- 10번 칸: 파이프라인 상태 ----------

function renderPipe(s) {
  const pill = $("pipe-pill");
  const passed = s.checks.filter((c) => c.ok).length;
  pill.className = "pill " + (s.ok ? "ok" : "bad");
  pill.textContent = s.ok ? "파이프라인 검사 통과" : "파이프라인 검사 실패";
  const when = s.exported_at ? s.exported_at.slice(0, 16).replace("T", " ") : "–";
  $("pipe-line").textContent = s.ok
    ? `내보낸 때 ${when}, 데이터 ${s.window[0]} ~ ${s.window[1]}, 검사 ${passed}/${s.checks.length} 통과, 기준 종목 ${s.bench}`
    : `내보낸 때 ${when}. 검사를 통과하지 못해 숫자를 보이지 않습니다.`;
  // 화면 숫자가 지나온 길: 레이크 경로 대조(원천과 Gold를 한 행씩)가 있는 파일일 때만 보임
  const lake = s.checks.find((c) => c.name.startsWith("레이크 경로 시세"));
  const snap = s.checks.find((c) => c.name === "Gold 마트 스냅샷");
  const flow = $("pipe-flow");
  flow.hidden = !lake;
  if (lake) {
    const steps = [["원천 PostgreSQL", "주문, 체결, 시세, 환율"], ["Debezium, Kafka", "변경 이벤트"], ["Flink → Bronze", "Iceberg, 받은 그대로"],
      ["Spark MERGE → Silver", "키마다 현재 상태"], ["dbt → Gold 마트", snap ? snap.value.split(", ")[0].replace("mart_symbol_daily ", "") : "시세, 환율"],
      ["화면 파일", lake.value]];
    flow.replaceChildren(...steps.map(([t, sub]) => {
      const li = document.createElement("li");
      li.className = lake.ok ? "ok" : "bad";
      const b = document.createElement("b"); b.textContent = t;
      const em = document.createElement("em"); em.textContent = sub;
      li.append(b, em);
      return li;
    }));
  }
  // 내 매매가 지나온 길(저장 켠 서버에서 loaders/screen_trades.py --verify를 돌린 뒤에만)
  const tl = s.trades_lake;
  $("trade-lake").hidden = !tl;
  if (tl) {
    const gp = tl.gold_pnl_checked !== undefined && tl.gold_pnl_checked !== null;
    const ok = tl.executions_ok && tl.pnl_same === tl.pnl_checked && (!gp || tl.gold_pnl_same === tl.gold_pnl_checked);
    const steps = [["화면에 저장한 매매", `계좌 ${tl.account}`], ["원천 주문, 체결, 잔고", "loaders/screen_trades.py"],
      ["Debezium, Kafka", "변경 이벤트"], ["Flink → Bronze", "Iceberg"], ["Spark MERGE → Silver", "지운 줄은 지움 표시"],
      ["dbt → fct_execution", `체결 ${tl.executions_gold} / ${tl.executions_want}건 ${tl.executions_ok ? "같음" : "다름"}`],
      ...(gp ? [["dbt → 손익 마트", `pnl_position ${tl.gold_pnl_same} / ${tl.gold_pnl_checked}(계좌, 종목) 같음`]] : [])];
    $("trade-flow").replaceChildren(...steps.map(([t, sub]) => {
      const li = document.createElement("li");
      li.className = ok ? "ok" : "bad";
      const b = document.createElement("b"); b.textContent = t;
      const em = document.createElement("em"); em.textContent = sub;
      li.append(b, em);
      return li;
    }));
    $("trade-lake-at").textContent = `확인한 때 ${(tl.at || "").slice(0, 16).replace("T", " ")}`;
    $("trade-lake-note").textContent = (gp
      ? `Gold 손익 마트(pnl_position, 기준일 ${tl.gold_pnl_asof})가 화면 손익과 ${tl.gold_pnl_same} / ${tl.gold_pnl_checked}(계좌, 종목) 칸마다 같고, `
      : "") + `Gold 체결만으로 다시 센 손익도 화면 손익과 ${tl.pnl_same} / ${tl.pnl_checked}(계좌, 종목) 같습니다. 계좌는 증권사마다 하나예요.` +
      (tl.stale ? " 확인한 뒤 매매를 고쳐, 새로 고친 매매는 아직 레이크에서 확인하지 않았습니다. " +
        "Airflow DAG lakehouse_refresh가 켜져 있으면 30분 안에 옮기고 확인합니다(직접 하려면 make trades-to-source)." : "");
    $("trade-lake-note").classList.toggle("err", Boolean(tl.stale) || !ok);
  }
  $("pipe-src").hidden = !s.source;
  $("pipe-src").textContent = s.source ? `읽는 곳: ${s.source}` : "";
  const tb = $("pipe-rows");
  tb.replaceChildren(...s.checks.map((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td></td><td></td><td><span class="pill ${c.ok ? "ok" : "bad"}">${c.ok ? "통과" : "실패"}</span></td>`;
    tr.children[0].textContent = c.name;
    tr.children[1].textContent = c.value + (c.detail ? ` (${c.detail})` : "");
    return tr;
  }));
  const fail = $("pipe-fail");
  fail.hidden = s.ok;
  if (!s.ok) {
    fail.innerHTML = "<b>실패한 검사</b><ul></ul><span>원천을 확인한 뒤 <span class=\"mono\">make screen-data</span>로 다시 내보내고 서버를 다시 띄우세요.</span>";
    fail.querySelector("ul").replaceChildren(...s.failures.map((f) => {
      const li = document.createElement("li");
      li.textContent = `${f.name}: ${f.value}${f.detail ? ` (${f.detail})` : ""}`;
      return li;
    }));
    $("pipe-more").open = s.checks.length > 0;
    $("pipe-more").hidden = !s.checks.length;
    $("main").hidden = true;
  }
}

async function pipelineFailed() {
  const { body } = await api("/api/status");
  if (body) renderPipe(body);
}

// ---------- 1번 칸: 매매 입력 ----------

const isUSD = () => st.symbols.get(st.sym)?.currency !== "KRW";
const unitName = () => (isUSD() ? "달러" : "원");
const rowNo = (sentIdx) => st.map[sentIdx] + 1;

function rowEl(r, i) {
  const n = i + 1;
  const div = document.createElement("div");
  div.className = "lrow";
  div.setAttribute("role", "row");
  div.dataset.i = i;
  const [lo, hi] = st.calendar;
  const unit = unitName();
  div.innerHTML = `
    <div class="cell datecell" role="cell"><label for="d${i}">날짜</label><input id="d${i}" data-k="date" type="date" min="${lo}" max="${hi}" aria-label="${n}번 줄 날짜"><span class="holdtag">앱 잔고 그대로</span></div>
    <div class="cell" role="cell"><label for="s${i}">구분</label><select id="s${i}" data-k="side" aria-label="${n}번 줄 구분"><option value="BUY">매수</option><option value="SELL">매도</option><option value="HOLD">지금 잔고</option></select></div>
    <div class="cell" role="cell"><label for="b${i}">증권사</label><select id="b${i}" data-k="broker" aria-label="${n}번 줄 증권사"><option value="">안 고름</option>${st.brokers.map((b) => `<option value="${b.code}">${b.name}</option>`).join("")}</select></div>
    <div class="cell" role="cell"><label for="q${i}">수량(주)</label><input id="q${i}" data-k="qty" inputmode="decimal" autocomplete="off" aria-label="${n}번 줄 수량"></div>
    <div class="cell" role="cell"><label for="p${i}">체결가(${unit})</label><input id="p${i}" data-k="price" inputmode="decimal" autocomplete="off" aria-label="${n}번 줄 체결가(${unit})"></div>
    <div class="cell fxcell" role="cell"><label for="f${i}">환율(원)</label><input id="f${i}" data-k="fx_rate" inputmode="decimal" autocomplete="off" aria-label="${n}번 줄 환율(원)"></div>
    <div class="cell avgcell" role="cell"><label>이 매매 뒤 평균 단가(${unit})</label><span class="avg" aria-label="${n}번 줄 뒤 평균 단가"></span></div>
    <div class="cell rmcell" role="cell"><button type="button" class="btn rm" aria-label="${n}번 줄 빼기">빼기</button></div>
    <p class="rowmsg" id="m${i}" hidden></p>`;
  for (const inp of div.querySelectorAll("[data-k]")) {
    inp.setAttribute("aria-describedby", `m${i}`);
    inp.value = r[inp.dataset.k] ?? (inp.dataset.k === "side" ? "BUY" : "");
    // 처음 연 값을 속성에도 둡니다(화면 값 대조가 저장 파일에서 다시 열린 값을 DOM으로 읽음)
    if (inp.tagName === "SELECT") for (const o of inp.options) o.defaultSelected = o.value === inp.value;
    else inp.defaultValue = r[inp.dataset.k] ?? "";
    inp.addEventListener("input", () => {
      st.rows[i][inp.dataset.k] = inp.value.trim();
      if (inp.dataset.k === "side") holdMode(div, i);
      changed();
    });
  }
  div.querySelector(".rm").addEventListener("click", () => removeRow(i));
  holdMode(div, i);
  return div;
}

// 지금 잔고 줄(구분 HOLD): 날짜 칸 대신 「앱 잔고 그대로」, 체결가 칸은 평균 단가, 환율 칸은 평균 매입 환율
function holdMode(div, i) {
  const hold = isHold(st.rows[i]), n = i + 1, unit = unitName();
  div.classList.toggle("hold", hold);
  const d = div.querySelector('[data-k="date"]');
  d.disabled = hold;
  d.tabIndex = hold ? -1 : 0;
  const names = hold ? [`평균 단가(${unit})`, "평균 매입 환율(원)"] : [`체결가(${unit})`, "환율(원)"];
  for (const [k, name] of [["price", names[0]], ["fx_rate", names[1]]]) {
    const inp = div.querySelector(`[data-k="${k}"]`);
    inp.setAttribute("aria-label", `${n}번 줄 ${name}`);
    inp.closest(".cell").querySelector("label").textContent = name;
    if (hold) inp.placeholder = k === "price" ? "평균 단가" : "매입 환율";
  }
}

function renderRows() {
  $("rows").replaceChildren(...st.rows.map(rowEl));
  document.querySelector(".ledger").classList.toggle("no-fx", !isUSD());
  $("h-price").textContent = `체결가(${unitName()})`;
  $("h-avg").textContent = `이 매매 뒤 평균 단가(${unitName()})`;
}

function addRow() {
  const last = st.rows[st.rows.length - 1];
  // 날짜와 증권사는 앞 줄을 따름(같은 증권사의 매매를 이어 넣는 경우가 많음)
  st.rows.push({ date: (last && !isHold(last) && last.date) || "", side: "BUY", qty: "", price: "", fx_rate: "",
    ...(last?.broker ? { broker: last.broker } : {}) });
  renderRows();
  $(`d${st.rows.length - 1}`).focus();
  changed();
}

// 지금 잔고 줄 넣기(기간 전 매매에 붙은 안내의 단추): 그 증권사를 채운 지금 잔고 줄을 맨 아래에 더하고 수량 칸으로
function addHoldRow(broker) {
  // 그 증권사의 지금 잔고 줄이 이미 있으면(아직 덜 채움) 새로 더하지 않고 그 줄 수량 칸으로(증권사마다 하나)
  const have = st.rows.findIndex((r) => isHold(r) && (r.broker || "") === (broker || ""));
  if (have >= 0) { $(`q${have}`).focus(); return; }
  st.rows.push({ side: "HOLD", qty: "", price: "", fx_rate: "", ...(broker ? { broker } : {}) });
  renderRows();
  $(`q${st.rows.length - 1}`).focus();
  changed();
}

function removeRow(i) {
  st.rows.splice(i, 1);
  renderRows();
  const next = $(`d${Math.min(i, st.rows.length - 1)}`);
  (next || $("add")).focus();
  changed();
}

function clean(v) {
  return (v || "").replace(/[,\s]/g, "");
}

// 서버와 같은 규칙으로 먼저 봅니다(서버도 다시 검사). 틀린 줄이 있으면 계산하지 않고 그 줄을 알립니다.
function check(r) {
  if (isHold(r)) {
    const need = isUSD() ? "수량, 평균 단가, 평균 매입 환율" : "수량과 평균 단가";
    if (!["qty", "price", "fx_rate"].some((k) => r[k])) return { skip: true, note: `증권사 앱 잔고 화면의 ${need}을 넣으면 계산에 넣습니다` };
    if (!r.qty || !r.price || (isUSD() && !r.fx_rate)) return { skip: true, note: `${need}을 모두 넣으면 계산에 넣습니다` };
    const q = clean(r.qty);
    if (!/^\d+$/.test(q) || Number(q) <= 0) return { err: "지금 잔고 수량은 0보다 큰 정수로 넣어 주세요", key: "qty" };
    for (const [k, label] of [["price", "평균 단가"], ["fx_rate", "평균 매입 환율"]]) {
      if (k === "fx_rate" && !isUSD()) continue;
      const v = clean(r[k]);
      if (!PRICE.test(v) || Number(v) <= 0) return { err: `${label}는 0보다 큰 수, 소수 넷째 자리까지입니다`, key: k };
    }
    return {};
  }
  const filled = ["date", "qty", "price", "fx_rate"].some((k) => r[k]);
  if (!filled) return { skip: true };
  if (!r.date || !r.qty) return { skip: true, note: "날짜와 수량을 넣으면 계산에 넣습니다" };
  const q = clean(r.qty).replace(/^-/, "");
  if (!NUM.test(q)) return { err: "수량은 수로 넣어 주세요", key: "qty" };
  for (const [k, label] of [["price", "체결가"], ["fx_rate", "환율"]]) {
    if (k === "fx_rate" && !isUSD()) continue;
    const v = clean(r[k]);
    if (v && (!PRICE.test(v) || Number(v) <= 0)) return { err: `${label}는 0보다 큰 수, 소수 넷째 자리까지입니다`, key: k };
  }
  return {};
}

function changed() {
  st.example = false;
  clearTimeout(st.timer);
  st.timer = setTimeout(run, 250);
  clearTimeout(st.saveTimer);
  st.saveTimer = setTimeout(save, 700);
}

// 줄 표시. badKey를 주면 그 칸에 aria-invalid를 켭니다(줄 문구는 aria-describedby로 이어져 있음).
function decorate(i, cls, msg, avgText, badKey) {
  const div = $("rows").children[i];
  if (!div) return;
  div.classList.remove("warnrow", "badrow", "out");
  if (cls) div.classList.add(cls);
  const p = div.querySelector(".rowmsg");
  p.hidden = !msg;
  p.textContent = msg || "";
  if (avgText !== undefined) div.querySelector(".avg").textContent = avgText;
  for (const inp of div.querySelectorAll("input")) {
    if (badKey && inp.dataset.k === badKey) inp.setAttribute("aria-invalid", "true");
    else inp.removeAttribute("aria-invalid");
  }
}

// 결과 칸(매매 칸 위의 요약과 아래의 그림, 매도): 지금 입력의 결과가 없으면 숨기고, 고치는 중이면 흐리게 두고 그렇다고 적습니다.
function showResult(mode) {
  for (const id of ["result", "result-more"]) {
    const box = $(id);
    box.hidden = mode === "none";
    box.classList.toggle("stale", mode === "stale");
  }
  for (const id of ["stale-note", "stale-note-2"]) $(id).hidden = mode !== "stale";
}

function setState(text, err = false) {
  const s = $("state");
  s.textContent = text;
  s.classList.toggle("err", err);
}

// ---------- 계산 ----------

const KEY_OF = { "날짜": "date", "수량": "qty", "체결가": "price", "환율": "fx_rate", "평균 단가": "price", "평균 매입 환율": "fx_rate" };

// 지금 입력으로 계산할 수 없을 때: 앞 결과가 있으면 흐리게 두고, 없으면 숨깁니다. 경고 목록은 앞 결과의 것이라 비웁니다.
function hold(text) {
  st.notes = null;
  showResult(st.res ? "stale" : "none");
  $("warnings").replaceChildren();
  setState(st.saveErr ? `${text} ${st.saveErr}.` : text, true);
}

// 계산 결과 줄(st.notes)에 저장 상태를 붙여 보입니다
function showNotes() {
  const parts = [...st.notes];
  if (st.saveErr) parts.push(st.saveErr);
  else if (st.savedAt) parts.push(`저장 ${st.savedAt}`);
  setState(parts.join(". ") + ".", Boolean(st.saveErr));
}

async function run() {
  // 입력이 바뀌었으니 앞에서 보낸 요청의 답은 버립니다. 일찍 끝나는 갈래(틀린 줄, 보낼 줄 없음)도 마찬가지입니다.
  const my = ++st.seq;
  st.ctl?.abort();
  st.ctl = null;
  const sent = [], map = [], errs = [];
  let pending = 0;
  st.rows.forEach((r, i) => {
    const c = check(r);
    if (c.err) { decorate(i, "badrow", c.err, "", c.key); errs.push(`${i + 1}번 줄 ${c.err}`); return; }
    if (c.skip) { decorate(i, null, c.note || "", ""); if (c.note) pending++; return; }
    const t = isHold(r) ? { side: "HOLD", qty: clean(r.qty) } : { date: r.date, side: r.side || "BUY", qty: clean(r.qty) };
    if (clean(r.price)) t.price = clean(r.price);
    if (isUSD() && clean(r.fx_rate)) t.fx_rate = clean(r.fx_rate);
    if (r.broker) t.broker = r.broker;
    sent.push(t); map.push(i);
  });
  if (st.asofErr) errs.unshift(st.asofErr);
  if (errs.length) { hold(`${errs.join(". ")}. 고치면 다시 계산합니다.`); return; }
  if (!sent.length) {
    // 넣는 중인 줄만 남았으면 앞 결과(같은 종목, 종목을 바꾸면 load가 지움)를 흐리게 둡니다. 결과 칸이 매매 칸 위에 있어
    // 숨겼다 다시 보이면 매매 칸이 위아래로 크게 밀리기 때문입니다. 줄이 하나도 없으면 숨깁니다
    if (pending && st.res) {
      showResult("stale");
    } else {
      st.res = null;
      showResult("none");
    }
    $("warnings").replaceChildren();
    st.notes = [pending ? "날짜와 수량을 넣으면 계산해요" : "매매를 넣으면 계산해요"];
    showNotes();
    return;
  }
  st.ctl = new AbortController();
  st.notes = null;
  setState("계산하는 중");
  let r;
  try {
    const resp = await fetch("/api/pnl", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: st.ctl.signal,
      body: JSON.stringify({ symbol: st.sym, asof: st.asof, trades: sent }),
    });
    r = { status: resp.status, body: await resp.json().catch(() => null) };
  } catch (e) {
    if (e.name === "AbortError" || my !== st.seq) return;
    hold("서버에 닿지 못했습니다. make screen으로 서버가 떠 있는지 확인하세요.");
    return;
  }
  if (my !== st.seq) return;
  if (r.status === 200) {
    st.res = r.body; st.map = map;
    render(pending);
  } else if (r.status === 503) {
    await pipelineFailed();
  } else if (r.status === 400 && r.body) {
    const k = r.body.trade_index;
    if (k !== undefined && map[k] !== undefined) {
      const msg = r.body.message.replace(/\d+번 줄/, `${map[k] + 1}번 줄`);
      const m = msg.match(/번 줄(?:\(지금 잔고\))?의 (날짜|수량|체결가|환율|평균 단가|평균 매입 환율)/);
      decorate(map[k], "badrow", msg, "", m ? KEY_OF[m[1]] : undefined);
      hold(msg);
    } else {
      hold(r.body.message || "입력을 읽지 못했습니다.");
    }
  } else {
    hold(`계산하지 못했습니다(${r.status}${r.body?.message ? ", " + r.body.message : ""}).`);
  }
}

// ---------- 결과 보이기 ----------

function render(pending) {
  const res = st.res, cur = res.currency;
  showResult("ok");
  // 경고: 줄마다 모아 그 줄에 붙이고, 아래에 목록으로도 보임
  const byRow = new Map();
  for (const w of res.warnings) {
    const i = st.map[w.trade_index];
    byRow.set(i, [...(byRow.get(i) || []), w]);
  }
  const stepByRow = new Map(res.steps.map((s) => [st.map[s.trade_index], s]));
  // 계산 안내(지금 잔고를 어떻게 셌는지 등): 줄에 붙이고 아래 목록에도. 거꾸로 셀 수 없었던 안내는 경고처럼 표시
  const noteByRow = new Map();
  for (const n of res.notes || []) {
    if (n.trade_index === null || n.trade_index === undefined) continue;
    const i = st.map[n.trade_index];
    noteByRow.set(i, [...(noteByRow.get(i) || []), n]);
  }
  const BAD_NOTE = new Set(["HOLD_TOO_FEW", "HOLD_SOLD_OUT", "HOLD_BAD_COST", "HOLD_COST_MISMATCH"]);
  st.rows.forEach((r, i) => {
    if (!st.map.includes(i)) return;
    const s = stepByRow.get(i), ws = byRow.get(i) || [], ns = noteByRow.get(i) || [];
    const avg = !s ? (isHold(r) && ns.some((n) => n.code === "HOLD_EXPLAINED") ? "첫날 전 보유 없음" : "계산에서 빠짐")
      : s.opening ? `첫날 ${num(s.qty)}주, ${price(s.price, cur)}`
      : (s.excluded ? "계산에서 빠짐" : s.qty_after > 0 ? price(s.avg_cost_after, cur) : "남은 수량 없음");
    const bad = ws.length || ns.some((n) => BAD_NOTE.has(n.code));
    decorate(i, bad ? "warnrow" : null, [...ws, ...ns].map((w) => w.message).join(" "), avg);
    const div = $("rows").children[i];
    const need = ns.find((n) => n.code === "PRE_WINDOW_NEEDS_HOLD");
    if (need) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "x-ghost sm addhold";
      b.textContent = `${need.broker_name} 지금 잔고 줄 넣기`;
      b.addEventListener("click", () => addHoldRow(need.broker));
      div.querySelector(".rowmsg").append(" ", b);
    }
    if (!s) div.classList.add("out");
    if (isHold(r)) return;
    // 비운 칸에는 계산에 쓴 값을 흐리게 보임(서버가 채운 그날 종가와 매매기준율). 계산에서 빠진 줄은 비움
    const used = s && !s.excluded;
    div.querySelector('[data-k="price"]').placeholder = used && s.price !== null ? price(s.price, cur) : "";
    div.querySelector('[data-k="fx_rate"]').placeholder = used && isUSD() && s.fx ? num(s.fx) : "";
  });
  $("warnings").replaceChildren(...res.warnings.map((w) => {
    const li = document.createElement("li");
    li.innerHTML = `<span></span><span class="code"></span>`;
    li.children[0].textContent = `${rowNo(w.trade_index)}번 줄(${w.trade_date}): ${w.message}`;
    li.children[1].textContent = w.code;
    return li;
  }));
  $("hold-notes").replaceChildren(...(res.notes || []).map((n) => {
    const li = document.createElement("li");
    li.innerHTML = `<span></span><span class="code"></span>`;
    const where = n.trade_index === null || n.trade_index === undefined ? "" : `${rowNo(n.trade_index)}번 줄(${n.broker_name}): `;
    li.children[0].textContent = where + n.message;
    li.children[1].textContent = n.code;
    return li;
  }));

  const notes = [`계산 ${res.ms}ms`];
  if (res.asof_price_date && res.asof_price_date !== res.asof) {
    notes.push(`기준일이 이 종목의 거래일이 아니라, 기준일 이전 마지막 거래일 ${res.asof_price_date}의 종가로 평가했습니다`);
  }
  if (res.warnings.length) notes.push(`입력 경고 ${res.warnings.length}건(아래 목록)`);
  if ((res.notes || []).length) notes.push(`계산 안내 ${res.notes.length}건(아래 목록)`);
  if (pending) notes.push(`넣는 중인 줄 ${pending}개는 아직 계산에 넣지 않았습니다`);
  st.notes = notes;
  showNotes();

  renderVerdict(res);
  renderKpis(res);
  renderBench(res);
  renderAccounts(res);
  renderEffects(res);
  renderSells(res);
  $("px-unit").textContent = cur === "KRW" ? "원" : "달러";
  $("lg-sym").textContent = `${res.symbol} 종가`;
  drawCharts();
  renderBigMoves(res);
  renderEvents(res);
}

function drawCharts() {
  if (!st.res) return;
  $("px-desc").textContent = drawPrice($("px"), $("px-read"), st.res, rowNo, openEvents);
  $("pl-desc").textContent = drawPnl($("pl"), $("pl-read"), st.res);
  $("lg-bench").hidden = Boolean(st.res.opening);
}

function setKpi(id, text, cls, sub, exact) {
  const v = $(id);
  v.textContent = text;
  // 줄인 금액이면 정확한 값을 title과 작은 줄 맨 앞에 둠
  if (exact) v.title = exact; else v.removeAttribute("title");
  if (exact && sub !== undefined) sub = sub ? `${exact}, ${sub}` : exact;
  // 긴 금액은 숫자 중간에서 줄이 나뉘지 않게 글자를 줄입니다(375px 폭의 두 칸 카드)
  const size = text.length > 14 ? " longer" : text.length > 11 ? " long" : "";
  v.className = "value " + (cls || "") + size;
  if (sub !== undefined) $(id + "-s").textContent = sub;
}

function renderKpis(res) {
  const s = res.summary, cur = res.currency;
  $("k-local-l").textContent = cur === "KRW" ? "단순 수익률" : "달러 손익";
  if (!s) {
    for (const id of ["k-krw", "k-local", "k-bench", "k-birr", "k-mdd", "k-irr"]) setKpi(id, "–", "na", "");
    $("k-krw-s").textContent = "계산에 들어간 매매가 없어요(내 매매 탭의 경고를 봐 주세요)";
    $("holding-line").replaceChildren();
    return;
  }
  // 큰 숫자 아래 작은 줄: 줄인 금액이면 정확한 값, 그리고 단순 수익률(토스증권식 「+2,650,314원 (단순 수익률 +24.08%)」), 실현과 평가
  const head = [shortened(s.total_krw) ? krw(s.total_krw, true) : "",
    s.simple_return_pct === null ? "" : `(단순 수익률 ${pct(s.simple_return_pct)})`].filter(Boolean).join(" ");
  setKpi("k-krw", krwBig(s.total_krw, true), tone(s.total_krw),
    `${head ? head + ". " : ""}실현 ${krw(s.realized_krw, true)}, 평가 ${krw(s.unrealized_krw, true)}`);
  if (cur === "KRW") {
    setKpi("k-local", pct(s.simple_return_pct), tone(s.simple_return_pct), `원화 손익 / 넣은 돈 ${krw(s.invested_krw)}`);
  } else {
    setKpi("k-local", local(s.total_local, cur, true), tone(s.total_local),
      `실현 ${local(s.realized_local, cur, true)}, 평가 ${local(s.unrealized_local, cur, true)}`);
  }
  if (res.opening) {
    setKpi("k-bench", "세지 않음", "na", "데이터 첫날 전부터 가진 주식이 있어 같은 날 같은 돈으로 견줄 수 없어요");
    setKpi("k-birr", "세지 않음", "na", "");
  } else {
    setKpi("k-bench", krwBig(s.total_minus_bench_krw, true), tone(s.total_minus_bench_krw),
      `SPY에 넣었다면 ${krw(s.bench_total_krw, true)}${res.symbol === res.bench ? "(이 종목이 SPY)" : ""}`,
      shortened(s.total_minus_bench_krw) ? krw(s.total_minus_bench_krw, true) : undefined);
    if (s.bench_irr_pct === null) setKpi("k-birr", "계산 못 함", "na", "이 매매의 연 환산과 같은 까닭으로 세지 않아요");
    else setKpi("k-birr", pct(s.bench_irr_pct), tone(s.bench_irr_pct), "같은 날 같은 돈을 SPY에 넣고 뺐을 때");
  }
  if (s.max_drawdown_krw === 0) setKpi("k-mdd", "없음", "zero", "누적 손익이 앞선 고점 아래로 내려간 날이 없어요");
  else setKpi("k-mdd", krwBig(s.max_drawdown_krw), "down", "앞선 고점에서 가장 크게 내려간 폭",
    shortened(s.max_drawdown_krw) ? krw(s.max_drawdown_krw) : undefined);
  if (res.opening) {
    setKpi("k-irr", "세지 않음", "na", "데이터 첫날 전 보유를 언제 얼마에 샀는지 몰라 세지 않아요");
  } else if (s.irr_pct === null) {
    setKpi("k-irr", "계산 못 함", "na", res.irr_short
      ? "첫 매매일부터 기준일까지 30일이 안 되어 세지 않아요"
      : "현금 흐름으로 풀리는 값이 없어요");
  }
  else setKpi("k-irr", pct(s.irr_pct), tone(s.irr_pct), "돈이 들어가고 나온 때를 감안한 한 해 수익률");

  const items = [];
  if (!res.booked) items.push(["장부에 들어간 매수가 없어요", null]);
  items.push(["남은 수량", `${num(s.qty_left)}주`]);
  if (res.multi && s.qty_left > 0) items.push(["평균 단가", "증권사마다 달라요(아래 증권사별)"]);
  if (!res.multi && s.qty_left > 0) items.push(["평균 단가", local(s.avg_cost_local, cur)]);
  if (!res.multi && cur !== "KRW" && s.qty_left > 0) items.push(["평균 매수 환율", `${num(s.avg_buy_fx)}원`]);
  if (!res.multi && s.holding_return_pct !== null && s.holding_return_pct !== undefined) {
    items.push(["남은 주식 수익률(증권사 앱의 수익률)", pct(s.holding_return_pct)
      + (cur !== "KRW" && s.holding_return_krw_pct !== null ? `, 원화로 ${pct(s.holding_return_krw_pct)}` : "")]);
  }
  items.push([res.opening ? "넣은 원화(첫날 전 보유는 그 원가)" : "넣은 원화", krw(s.invested_krw)], ["단순 수익률", pct(s.simple_return_pct)]);
  if (res.holding) items.push([res.opening ? "보유 기간(데이터 첫날 전부터 ~ 기준일)" : "보유 기간(첫 매수일 ~ 기준일)", `${res.holding[0]} ~ ${res.holding[1]}`]);
  $("holding-line").replaceChildren(...items.map(([k, v]) => {
    const span = document.createElement("span");
    span.textContent = v === null ? k : `${k} `;
    if (v !== null) { const b = document.createElement("b"); b.textContent = v; span.appendChild(b); }
    return span;
  }));
}

// 증권사별(계좌마다 이동평균): 증권사를 고른 줄이 있거나 지금 잔고 줄이 있을 때. 큰 숫자는 증권사 앱의 「수익률」과 같은
// 남은 주식 수익률(남은 주식의 평가 손익 / 남은 매입 금액, 현지 통화)
function renderAccounts(res) {
  const box = $("acct-box"), list = $("acct-list"), cur = res.currency;
  const accts = res.accounts || [];
  const show = res.summary && (res.multi || res.opening || accts.some((a) => a.broker));
  box.hidden = !show;
  if (!show) { list.replaceChildren(); return; }
  list.replaceChildren(...accts.map((a) => {
    const s = a.summary;
    const div = document.createElement("div");
    div.className = "li";
    div.setAttribute("role", "listitem");
    div.innerHTML = `<div class="li-l"><b></b><span class="sub"></span></div><span class="value"></span>`;
    div.querySelector("b").textContent = a.broker_name;
    const parts = [];
    const none = !s || !s.invested_krw;  // 장부에 들어간 매수가 없음(기간 전 매수와 초과 매도뿐 등)
    if (none) parts.push("계산에 들어간 매수가 없어요");
    else {
      parts.push(`남은 ${num(s.qty_left)}주`);
      if (s.qty_left > 0) parts.push(`평균 단가 ${local(s.avg_cost_local, cur)}`);
      if (s.qty_left > 0 && cur !== "KRW") parts.push(`매입 환율 ${num(s.avg_buy_fx)}원`);
      parts.push(`실현 ${krw(s.realized_krw, true)}`, `손익 ${krw(s.total_krw, true)}`);
      if (a.opening) parts.push(`데이터 첫날 보유 ${num(a.opening.qty)}주로 시작`);
    }
    div.querySelector(".sub").textContent = parts.join(", ");
    const v = div.querySelector(".value");
    if (a.holding_return_pct === null) { v.textContent = !none && s.qty_left === 0 ? "모두 팖" : "–"; v.className = "value na"; }
    else {
      v.textContent = pct(a.holding_return_pct);
      v.className = "value " + tone(a.holding_return_pct);
      if (cur !== "KRW" && a.holding_return_krw_pct !== null) v.title = `원화로 ${pct(a.holding_return_krw_pct)}`;
    }
    return div;
  }));
  const notes = ["오른쪽 숫자는 증권사 앱의 「수익률」처럼 남은 주식의 평가 손익을 남은 매입 금액으로 나눈 값이에요(현지 통화, 기준일 종가)."];
  if (res.multi) notes.push("증권사마다 평균 단가를 따로 셉니다. 위 원화 손익의 실현과 평가는 증권사별 합이고, 합계는 모두 합쳐 셈한 값과 같아요.");
  if (cur !== "KRW") notes.push("미국 종목은 숫자에 마우스를 올리면 원화 기준 수익률이 보여요.");
  $("acct-note").textContent = notes.join(" ");
}

// 국내 종목은 환율 효과와 교차 효과가 0이라 나누기 칸 대신 결론 문장 한 줄로 적습니다
const priceOnly = (res) => res.currency === "KRW" && res.summary.fx_effect_krw === 0 && res.summary.cross_effect_krw === 0;

// 결론 문장: 요약 카드와 나누기 칸의 API 값을 문장으로 옮깁니다(다시 계산하지 않고, 차이는 부호만 뗌)
function renderVerdict(res) {
  // 원화 손익은 위의 큰 숫자(k-krw)가 맡고, 이 문장은 손익이 어디서 왔는지와 SPY 비교만(해요체). 금액은 줄여 쓰고
  // 정확한 값은 마우스를 올리면(title) 보이고, 아래 목록과 나누기 칸에 원 단위로 있음
  const p = $("verdict"), s = res.summary;
  p.replaceChildren();
  p.hidden = !s;
  if (!s) return;
  const n = (text, v, exact) => {
    const b = document.createElement("b");
    b.className = "n " + tone(v);
    b.textContent = text;
    if (exact && exact !== text) b.title = exact;
    p.append(b);
  };
  if (!res.booked) {
    const need = (res.notes || []).find((n) => n.code === "PRE_WINDOW_NEEDS_HOLD");
    if (need) {
      // 기간 전 매매만 있는 종목(장기 보유): 지금 잔고 줄로 가는 길을 요약에서도 보임
      p.append("데이터 기간 전 매매만 있어 아직 셀 수 없어요. 「지금 잔고」 줄에 증권사 앱 잔고를 넣으면 셀 수 있어요. ");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "x-ghost sm addhold";
      b.dataset.goto = "trade";
      b.textContent = `${need.broker_name} 지금 잔고 줄 넣기`;
      // 탭 바꾸기(ui.js, 문서에서 받음)가 먼저 끝난 뒤 줄을 더해야 새 줄 수량 칸에 초점이 감
      b.addEventListener("click", () => setTimeout(() => addHoldRow(need.broker), 0));
      p.append(b);
      return;
    }
    p.append("장부에 들어간 매수가 없어 손익을 말할 수 없어요. 내 매매 탭의 경고를 봐 주세요.");
    return;
  }
  if (priceOnly(res)) {
    p.append("국내 종목이라 환율 효과 없이 모두 가격 효과예요.");
  } else {
    p.append("가격 효과 ");
    n(krwBig(s.price_effect_krw, true), s.price_effect_krw, krw(s.price_effect_krw, true));
    p.append(", 환율 효과 ");
    n(krwBig(s.fx_effect_krw, true), s.fx_effect_krw, krw(s.fx_effect_krw, true));
    p.append(", 교차 효과 ");
    n(krwBig(s.cross_effect_krw, true), s.cross_effect_krw, krw(s.cross_effect_krw, true));
    const gap = s.price_effect_krw + s.fx_effect_krw + s.cross_effect_krw - s.total_krw;
    p.append(gap ? `으로 나뉘어요(원 단위 반올림으로 셋의 합과 ${krw(Math.abs(gap))} 차이).` : "으로 나뉘어요.");
  }
}

// SPY와 견주기 카드의 문장(전에 결론 문장 끝에 있던 SPY 비교, 11/19에 원화 손익 카드에서 뺌)
function renderBench(res) {
  const p = $("bench-line"), s = res.summary;
  p.replaceChildren();
  $("bench-box").hidden = !s || !res.booked;
  if (!s || !res.booked) return;
  if (res.symbol === res.bench) { p.textContent = "이 종목이 SPY라 견줄 것이 없어요."; return; }
  if (res.opening) { p.textContent = "데이터 첫날 전부터 가진 주식이 있어 SPY와는 견주지 않아요."; return; }
  const n = (text, v, exact) => {
    const b = document.createElement("b");
    b.className = "n " + tone(v);
    b.textContent = text;
    if (exact && exact !== text) b.title = exact;
    p.append(b);
  };
  const d = s.total_minus_bench_krw;
  p.append("같은 날 같은 돈을 SPY에 넣었다면 ");
  n(krwBig(s.bench_total_krw, true), s.bench_total_krw, krw(s.bench_total_krw, true));
  if (d === 0) { p.append("으로 이 매매와 같아요."); return; }
  p.append("이라, 이 매매가 ");
  n(krwBig(Math.abs(d)), d, krw(Math.abs(d)));
  p.append(d > 0 ? " 더 많아요." : " 더 적어요.");
}

function renderEffects(res) {
  const s = res.summary, box = $("fx");
  const off = !s || !res.booked || priceOnly(res);
  $("fx-box").hidden = off;
  if (off) { box.replaceChildren(); $("fx-note").textContent = ""; return; }
  const rows = [["가격 효과", s.price_effect_krw], ["환율 효과", s.fx_effect_krw], ["교차 효과", s.cross_effect_krw]];
  const max = Math.max(1, ...rows.map(([, v]) => Math.abs(v)), Math.abs(s.total_krw));
  const parts = [];
  for (const [name, v] of [...rows, ["원화 손익", s.total_krw]]) {
    const total = name === "원화 손익";
    const w = (Math.abs(v) / max) * 50;
    const bar = `<div class="fx-track"><i class="${v >= 0 ? "pos" : "neg"}" style="left:${v >= 0 ? 50 : 50 - w}%;width:${w}%"></i><span class="mid"></span></div>`;
    parts.push(`<span class="${total ? "fx-total" : ""}">${name}</span>`, total ? `<div class="fx-total">${bar}</div>` : bar,
      `<span class="v ${tone(v)} ${total ? "fx-total" : ""}">${krw(v, true)}</span>`);
  }
  box.innerHTML = parts.join("");
  const notes = [];
  const sum = s.price_effect_krw + s.fx_effect_krw + s.cross_effect_krw;
  if (sum !== s.total_krw) notes.push(`셋의 합(${krw(sum, true)})과 원화 손익은 원 단위 반올림으로 ${krw(Math.abs(sum - s.total_krw))} 다릅니다.`);
  if (res.currency !== "KRW" && s.naive_minus_total_krw !== 0) {
    notes.push(`달러 손익을 기준일 환율 하나로 바꾸면 ${krw(s.naive_krw, true)}으로, 실제 원화 손익보다 ${krw(Math.abs(s.naive_minus_total_krw))} ${s.naive_minus_total_krw > 0 ? "많게" : "적게"} 보입니다. 매매마다 그날 환율로 셈해야 하는 까닭입니다.`);
  }
  $("fx-note").textContent = notes.join(" ");
}

function renderSells(res) {
  const tb = $("sells"), cur = res.currency, unit = cur === "KRW" ? "원" : "달러";
  $("h-sp").textContent = `가격(${unit})`;
  $("h-sa").textContent = `팔기 전 평균 단가(${unit})`;
  // 매도가 없으면 표 대신 한 줄로 적습니다(빈 표가 칸을 차지하지 않게)
  const none = !res.sells.length;
  $("sells-box").hidden = none;
  $("sells-note").hidden = none;
  $("sells-empty").hidden = !none;
  // 넣은 매도 줄이 계산에서 빠졌는지(초과 매도, 기준일 뒤, 데이터 기간 밖 등): 장부에 남은 매도 단계가 없는 매도 줄
  const dropped = st.map.some((row, k) => (st.rows[row].side || "BUY") === "SELL"
    && !res.steps.some((x) => x.trade_index === k && !x.excluded));
  $("sells-empty").textContent = !none ? ""
    : dropped ? "계산에 들어간 매도가 없어요. 빠진 매도 줄은 내 매매 탭의 경고를 봐 주세요." : "아직 판 적이 없어요.";
  if (none) {
    tb.replaceChildren();
    return;
  }
  tb.replaceChildren(...res.sells.map((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td></td><td class="num"></td><td class="num"></td><td class="num"></td><td class="num"></td>`;
    // 증권사가 둘 이상이면 날짜 칸에 증권사 이름을 붙임(팔기 전 평균 단가가 그 증권사 계좌의 평단)
    const cells = [res.multi ? `${s.date} ${brokerName(s.broker)}` : s.date, `${num(s.qty)}주`, price(s.price, cur), price(s.avg_cost_before, cur), krw(s.realized_krw, true)];
    const labels = ["날짜", "수량", `가격(${unit})`, `팔기 전 평균 단가(${unit})`, "원화 손익"];
    cells.forEach((t, k) => { tr.children[k].textContent = t; tr.children[k].dataset.l = labels[k]; });
    tr.children[4].className = "num " + tone(s.realized_krw);
    return tr;
  }));
}

function renderBigMoves(res) {
  const bm = res.big_moves, list = $("bm-list"), sum = $("bm-sum");
  if (!bm) { sum.textContent = "큰 움직임 날: 장부에 들어간 매수가 없어 보유 기간이 없습니다"; list.replaceChildren(); return; }
  let text = `첫 매수일부터 기준일까지(${res.holding[0]} ~ ${res.holding[1]}) 큰 움직임 ${bm.dates.length}일`;
  if (bm.not_judged_in_holding) text += `, ${bm.judged_from ?? "데이터 끝"} 전은 판정 전`;
  sum.textContent = text;
  list.replaceChildren(...bm.dates.map((d) => { const li = document.createElement("li"); li.textContent = d; return li; }));
}

// ---------- 7번 칸: 그때 무슨 일이 ----------

const KIND = { fomc: "FOMC", cpi: "CPI", jobs: "고용 보고서" };

// 보유 기간의 큰 움직임 날을 단추로(최근 날이 앞). 고른 날이 이 결과에 없으면(종목이나 매매가 바뀜) 앞 답을 치웁니다
function renderEvents(res) {
  const bm = res.big_moves, box = $("ev-days");
  const kr = res.currency === "KRW";
  if (!bm || !bm.dates.length) {
    $("ev-sum").textContent = !bm ? "장부에 들어간 매수가 없어 보유 기간이 없어요." : "보유 기간에 큰 움직임 날이 없어요.";
  } else {
    $("ev-sum").textContent = `보유 기간에 크게 움직인 날이 ${bm.dates.length}일이에요. 날짜를 누르면 그날의 기사 요약과 일정이 나와요.` +
      (kr ? " 국내 종목은 기사 근거가 없어 움직임만 보여요(다음 판)." : "");
  }
  const dates = bm ? [...bm.dates].reverse() : [];
  box.replaceChildren(...dates.map((d) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip"; b.textContent = d; b.dataset.d = d;
    b.setAttribute("aria-pressed", st.evSel && st.evSel.symbol === res.symbol && st.evSel.date === d ? "true" : "false");
    b.disabled = st.evBusy;
    return b;
  }));
  // 날짜가 12개를 넘으면 최근 12개만 보이고 나머지는 단추로 펼침(고른 날이 접힌 쪽이면 펼친 채로)
  const FOLD = 12;
  const selIdx = st.evSel ? dates.indexOf(st.evSel.date) : -1;
  box.classList.toggle("fold", dates.length > FOLD && selIdx < FOLD);
  if (dates.length > FOLD) {
    const more = document.createElement("button");
    more.type = "button"; more.className = "ev-more";
    const label = () => (box.classList.contains("fold") ? `나머지 ${dates.length - FOLD}일 보기` : "최근 12일만 보기");
    more.textContent = label();
    more.setAttribute("aria-expanded", String(!box.classList.contains("fold")));
    more.addEventListener("click", (e) => { e.stopPropagation(); box.classList.toggle("fold"); more.textContent = label(); more.setAttribute("aria-expanded", String(!box.classList.contains("fold"))); });
    box.append(more);
  }
  if (!(st.evSel && st.evSel.symbol === res.symbol && dates.includes(st.evSel.date))) {
    st.evSel = null;
    st.eseq++;
    $("ev-card").hidden = true;
    if (!st.evBusy) evState("");
  }
}

// 종목을 바꾸면 앞 종목의 답과 고른 날을 치우고, 오는 중인 답은 버립니다(eseq)
function resetEvents() {
  st.evSel = null;
  st.eseq++;
  $("ev-card").hidden = true;
  evState("");
}

function evState(text, err = false) {
  const s = $("ev-state");
  s.textContent = text;
  s.classList.toggle("err", err);
}

function li(text, cls) {
  const e = document.createElement("li");
  if (cls) e.className = cls;
  e.textContent = text;
  return e;
}

// 가격 그림의 큰 움직임 눈금에서 열 때: 7번 칸으로 옮겨 가고 그날을 물음
function openEvents(day) {
  $("ev-box").scrollIntoView({ block: "start", behavior: "smooth" });
  askEvents(day);
}

async function askEvents(day) {
  if (st.evBusy || !st.res) return;
  const symbol = st.res.symbol, my = ++st.eseq;
  st.evBusy = true;
  st.evSel = { symbol, date: day };
  document.querySelectorAll("#ev-days .chip").forEach((b) => {
    b.disabled = true;
    b.setAttribute("aria-pressed", b.dataset.d === day ? "true" : "false");
  });
  $("ev-card").hidden = true;  // 앞 날짜의 답이 새 날짜의 답처럼 남지 않게
  evState("그날 기사를 찾고 정리하는 중입니다(처음 한 번은 몇 초).");
  let r;
  try {
    r = await api("/api/events", { method: "POST", body: JSON.stringify({ symbol, date: day }) });
  } catch {
    r = { status: 0, body: null };
  }
  st.evBusy = false;
  document.querySelectorAll("#ev-days .chip").forEach((b) => { b.disabled = false; });
  if (my !== st.eseq || !st.res || st.res.symbol !== symbol) return;  // 그사이 종목이나 고른 날이 바뀜
  if (r.status === 503 && r.body?.error === "PIPELINE_CHECK_FAILED") { await pipelineFailed(); return; }
  if (r.status !== 200 || !r.body) {
    evState(`답을 받지 못했습니다(${r.status || "서버에 닿지 못함"}${r.body?.message ? ", " + r.body.message : ""}).`, true);
    return;
  }
  const a = r.body, mv = a.move || {};
  $("ev-day").textContent = `${a.date} ${a.name}`;
  $("ev-pct").textContent = mv.pct === null || mv.pct === undefined ? "" : pct(mv.pct);
  $("ev-pct").className = "ev-pct " + tone(mv.pct);
  $("ev-pill").textContent = !mv.judged ? "판정 전" : mv.big ? `큰 움직임, 표준편차 ${mv.ratio}배` : `보통 날, 표준편차 ${mv.ratio}배`;
  $("ev-pill").className = "pill " + (!mv.judged ? "plain" : mv.big ? "warn" : "plain");
  $("ev-head").textContent = a.head || "";
  $("ev-body").textContent = a.body || "";
  $("ev-cites").replaceChildren(...a.citations.map((c) => {
    const e = li("", null);
    const t = document.createElement("span");
    t.textContent = `[${c.n}] ${c.title}`;
    const src = document.createElement("span");
    src.className = "src";
    src.textContent = ` ${c.publisher}, ${c.time.slice(0, 16).replace("T", " ")} UTC`;
    e.append(t, src);
    return e;
  }));
  $("ev-cites").hidden = !a.citations.length;
  $("ev-sched").replaceChildren(...a.schedule.map((e) => li(`${e.date} ${KIND[e.kind] || e.kind}: ${e.title}`)));
  $("ev-sched-box").hidden = !a.schedule.length && a.decided_by === "code:domestic";
  if (!a.schedule.length && a.decided_by !== "code:domestic") $("ev-sched").replaceChildren(li("이 창에 FOMC, CPI, 고용 보고서 발표가 없습니다."));
  const others = a.others || [];
  $("ev-others").replaceChildren(...others.map((o) => {
    const e = document.createElement("li");
    const n = document.createElement("span"); n.className = "n"; n.textContent = `${o.name}(${o.symbol})`;
    const v = document.createElement("span"); v.className = "v " + tone(o.pct);
    v.textContent = pct(o.pct) + (o.big ? ", 큰 움직임" : "");
    e.append(n, v);
    return e;
  }));
  $("ev-others-box").hidden = !others.length;
  const how = a.cached ? "저장해 둔 답(모델을 다시 부르지 않음)"
    : a.decided_by === "code:domestic" ? "모델 없이 코드가 정한 답"
    : `검색 판 H1, 창 안 기사 ${a.window_articles ?? "?"}건, 모델 호출 ${a.n_llm}번, ${a.seconds}초, 약 ${a.cost_usd}달러`;
  $("ev-meta").textContent = how;
  $("ev-card").hidden = false;
  evState("");
}

// ---------- 8번 칸: 앞으로의 범위 ----------

// 범위는 종목과 기준일로만 정해집니다. 기준일이 틀린 동안은 앞 범위를 흐리게 두고, 종목을 바꾸면 숨겼다 새로 보입니다
function showRange(mode) {
  const box = $("range-box");
  box.hidden = mode === "none";
  box.classList.toggle("stale", mode === "stale");
  $("rg-stale").hidden = mode !== "stale";
}

function holdRange() {
  st.rseq++;
  st.rctl?.abort();
  st.rctl = null;
  if (!$("range-box").hidden) showRange("stale");
}

async function fetchRange() {
  const my = ++st.rseq;
  st.rctl?.abort();
  st.rctl = new AbortController();
  let r;
  try {
    const q = `symbol=${encodeURIComponent(st.sym)}&asof=${encodeURIComponent(st.asof)}`;
    const resp = await fetch(`/api/range?${q}`, { signal: st.rctl.signal });
    r = { status: resp.status, body: await resp.json().catch(() => null) };
  } catch (e) {
    if (e.name === "AbortError" || my !== st.rseq) return;
    r = { status: 0, body: null };
  }
  if (my !== st.rseq) return;
  if (r.status === 503) { await pipelineFailed(); return; }
  if (r.status !== 200) {
    renderRange(null, `범위를 불러오지 못했습니다(${r.status || "서버에 닿지 못함"}${r.body?.message ? ", " + r.body.message : ""}).`);
    return;
  }
  renderRange(r.body);
}

// 서버가 정한 보일 값(범위 %는 소수 둘째 자리, 적중률은 소수 첫째 자리)을 그대로 옮깁니다.
// 이 칸의 문장은 설계서 2절(8번 칸)과 7절 5단계에서 정한 문장이라, 11/18 화면 개편의 해요체로 바꾸지 않고 그대로 둡니다
function renderRange(v, err) {
  const box = $("range-box"), flag = $("rg-flag");
  box.dataset.symbol = v ? v.symbol : "";
  box.dataset.base = v ? v.base_date : "";
  if (!v) {
    $("rg-unit").textContent = "";
    $("rg-range").textContent = "–";
    $("rg-sub").textContent = err;
    $("rg-hit").textContent = "";
    flag.hidden = true;
    showRange("ok");
    return;
  }
  $("rg-unit").textContent = `${v.currency === "KRW" ? "원" : "달러"} 가격, 앞으로 ${v.ahead}거래일`;
  if (v.ok) {
    $("rg-range").textContent = `${pct(v.lo_pct)} ~ ${pct(v.hi_pct)}`;
    $("rg-sub").textContent = `${v.base_date} 종가에서 ${v.ahead}거래일 뒤까지 가격 변화의 80% 범위입니다. 최근 ${v.past}일 변동성이 이어진다고 본 단순 계산입니다.`;
  } else {
    $("rg-range").textContent = "–";
    $("rg-sub").textContent = `${v.base_date} 앞에 일별 수익률 ${v.past}개가 없어 범위를 셀 수 없습니다.`;
  }
  // 데이터가 2년뿐이라 적중률은 판정이 아니라 참고 값입니다(설계서 2절 8번 칸)
  $("rg-hit").textContent = v.n
    ? `지난 ${v.n}번의 ${v.ahead}거래일 창에서 이 방법의 범위 안에 든 것은 ${v.hits}번(${num(v.hit_rate_pct, 1)}%)입니다. 데이터가 2년뿐이라 참고로만 보는 값입니다.`
    : "지난 창이 없어 적중률을 셀 수 없습니다.";
  // 알림은 어느 쪽으로 벗어났는지 나눠 적습니다. 지난 창이 하나도 없으면 위 문장으로 충분해 표본 문구를 숨깁니다
  flag.hidden = v.display === "없음" || !v.n;
  flag.className = "rg-flag " + (v.display === "알림" ? "warn" : "plain");
  flag.textContent = v.display === "알림"
    ? (v.side === "넓음"
      ? "적중률이 90%를 넘어, 이 종목에서는 80% 범위가 지난날의 움직임보다 넓었습니다."
      : "적중률이 70%에 못 미쳐, 이 종목에서는 80% 범위가 지난날의 움직임보다 좁았습니다.")
    : v.display === "표본 문구" && v.n ? `표본이 적어(n = ${v.n}) 적중률로 판단하기 어렵습니다.` : "";
  showRange("ok");
}

// ---------- 9번 칸: 묻고 답하기 ----------

// 화면에 넣은 매매 전체(종목마다, 지금 고치는 종목은 화면의 줄)를 계좌로 보냅니다. 날짜와 수량이 있는 줄만
function account() {
  const all = { ...st.saved.symbols, [st.sym]: snapshot() };
  const out = {};
  for (const [sym, v] of Object.entries(all)) {
    // 지금 잔고 줄은 날짜 없이, 수량과 평균 단가가 있으면 보냄(서버가 한 계좌로 합쳐 셈)
    const krw = st.symbols.get(sym)?.currency === "KRW";
    const rows = (v?.trades || []).filter((t) => (isHold(t) ? t.qty && t.price && (krw || t.fx_rate) : t.date && t.qty))
      .map((t) => ({ ...(isHold(t) ? {} : { date: t.date }), side: t.side || "BUY", qty: clean(t.qty),
        ...(clean(t.price) ? { price: clean(t.price) } : {}), ...(clean(t.fx_rate) ? { fx_rate: clean(t.fx_rate) } : {}),
        ...(t.broker ? { broker: t.broker } : {}) }));
    if (rows.length) out[sym] = rows;
  }
  return out;
}

// ---------- 전체 탭(11/19 중간 판 의견): 저장한 모든 종목을 지금 기준일로 다시 셈 ----------

function pfState(text, err = false) {
  const p = $("pf-state");
  p.textContent = text;
  p.classList.toggle("err", err);
}

async function fetchPortfolio() {
  if (!st.calendar || !st.asof) return;  // 주소가 #all이면 탭이 시작 전에 보여, 시작이 끝난 뒤(boot 끝) 다시 부름
  const my = ++st.pseq;
  const acct = account();  // 저장한 모든 종목 + 지금 고치는 종목(묻고 답하기와 같은 계좌)
  if (!Object.keys(acct).length) {
    for (const id of ["pf-list", "pf-brokers"]) $(id).replaceChildren();
    setKpi("pf-krw", "–", "na", "넣은 매매가 없어요. 종목을 골라 내 매매 탭에서 넣어 주세요.");
    $("pf-asof").textContent = "";
    $("pf-note").textContent = "";
    pfState("");
    return;
  }
  pfState("계산하는 중");
  let r;
  try {
    r = await api("/api/portfolio", { method: "POST", body: JSON.stringify({ asof: st.asof, account: acct }) });
  } catch {
    if (my === st.pseq) pfState("서버에 닿지 못했습니다. make screen으로 서버가 떠 있는지 확인하세요.", true);
    return;
  }
  if (my !== st.pseq) return;
  if (r.status !== 200) { pfState(`계산하지 못했습니다(${r.status}${r.body?.message ? ", " + r.body.message : ""}).`, true); return; }
  renderPortfolio(r.body);
}

function pfLi(name, sub, valueText, valueTone, small, onClick) {
  const el = document.createElement(onClick ? "button" : "div");
  el.className = "li" + (onClick ? " pf-row" : "");
  el.setAttribute("role", "listitem");
  if (onClick) { el.type = "button"; el.addEventListener("click", onClick); }
  el.innerHTML = `<div class="li-l"><b></b><span class="sub"></span></div><span class="value"></span>`;
  el.querySelector("b").textContent = name;
  el.querySelector(".sub").textContent = sub;
  const v = el.querySelector(".value");
  v.textContent = valueText;
  v.className = "value " + (valueTone || "");
  if (small) { const sm = document.createElement("small"); sm.textContent = small; v.append(sm); }
  return el;
}

function renderPortfolio(p) {
  const t = p.total;
  $("pf-asof").textContent = `기준일 ${p.asof}`;
  const head = shortened(t.total_krw) ? `${krw(t.total_krw, true)}, ` : "";
  setKpi("pf-krw", krwBig(t.total_krw, true), tone(t.total_krw),
    `${head}실현 ${krw(t.realized_krw, true)}, 평가 ${krw(t.unrealized_krw, true)}. 넣은 원화 ${krw(t.invested_krw)}, 평가액 ${krw(t.value_krw)}`
    + (t.simple_return_pct === null ? "" : `, 단순 수익률 ${pct(t.simple_return_pct)}`));
  $("pf-list").replaceChildren(...p.symbols.map((x) => {
    const parts = [x.brokers.join(", ")];
    if (!x.booked) parts.push(x.notes.includes("PRE_WINDOW_NEEDS_HOLD") ? "기간 전 매매만 있어 지금 잔고 줄이 필요해요" : "계산에 들어간 매수가 없어요");
    else parts.push(`남은 ${num(x.qty_left)}주`, `평가액 ${krw(x.value_krw)}` + (x.weight_pct === null ? "" : `(비중 ${one(x.weight_pct)}%)`));
    if (x.warnings) parts.push(`입력 경고 ${x.warnings}건`);
    const go = () => {
      if (st.sym !== x.symbol) { if (st.saveTimer) save(); resetEvents(); load(x.symbol); }
      document.querySelector('.tab[data-tab="sum"]').click();
    };
    return pfLi(`${x.name} ${x.symbol}`, parts.filter(Boolean).join(", "), x.booked ? krwBig(x.total_krw, true) : "–",
      x.booked ? tone(x.total_krw) : "na", x.booked && x.simple_return_pct !== null ? pct(x.simple_return_pct) : "", go);
  }));
  $("pf-brokers").replaceChildren(...p.brokers.map((b) => pfLi(b.broker_name,
    `종목 ${b.symbols}개, 평가액 ${krw(b.value_krw)}, 실현 ${krw(b.realized_krw, true)}, 평가 ${krw(b.unrealized_krw, true)}`,
    krwBig(b.total_krw, true), tone(b.total_krw))));
  const notes = ["저장한 모든 종목의 매매를 위 기준일로 다시 센 값이에요(종목마다 저장한 기준일과 다를 수 있어요).",
    "SPY 비교와 연 환산 수익률은 돈이 들어간 때가 종목마다 달라 여기서 더하지 않고 종목 요약에서 봐요."];
  if (p.symbols.some((x) => x.currency === "USD")) notes.push(`미국 종목 평가액은 ${p.fx_date} 매매기준율로 원화로 바꿨어요.`);
  if (p.skipped.length) notes.push(`틀린 줄이 있어 뺀 종목: ${p.skipped.map((x) => `${x.name}(${x.message})`).join(", ")}`);
  $("pf-note").textContent = notes.join(" ");
  pfState(`계산 ${p.ms}ms. 종목 ${p.symbols.length}개, 증권사 ${p.brokers.length}곳`);
}

const STATUS = { answered: ["답함", "ok"], unknown: ["모름", "plain"], refused: ["답하지 않음", "plain"], clarify: ["되물음", "warn"] };

function qaState(text, err = false) {
  const s = $("qa-state");
  s.textContent = text;
  s.classList.toggle("err", err);
}

async function ask(question) {
  if (st.asking) return;
  const q = question.trim();
  if (!q) { qaState("질문을 적어 주세요.", true); $("qa-q").focus(); return; }
  if (st.asofErr) { qaState("기준일을 고친 뒤 물어 주세요.", true); return; }
  st.asking = true;
  $("qa-go").disabled = true;
  qaState("답을 만드는 중입니다(보통 몇 초).");
  // 보낸 그대로를 답에 적어 둠(기다리는 동안 매매를 고쳐도 그 답이 무엇으로 셈했는지 남게)
  const acct = account(), nSym = Object.keys(acct).length, nRow = Object.values(acct).reduce((n, v) => n + v.length, 0);
  let r;
  try {
    r = await api("/api/ask", { method: "POST", body: JSON.stringify({ question: q, asof: st.asof, account: acct }) });
  } catch {
    r = { status: 0, body: null };
  }
  st.asking = false;
  $("qa-go").disabled = false;
  $("qa-q").focus();  // 단추가 막히며 잃은 초점을 질문 칸으로 돌려줌
  if (r.status === 503 && r.body?.error === "PIPELINE_CHECK_FAILED") { await pipelineFailed(); return; }
  if (r.status !== 200 || !r.body) {
    qaState(`답을 받지 못했습니다(${r.status || "서버에 닿지 못함"}${r.body?.message ? ", " + r.body.message : ""}).`, true);
    return;
  }
  const a = r.body, [label, cls] = STATUS[a.status] || [a.status, "plain"];
  const li = document.createElement("li");
  li.innerHTML = `<p class="qa-q"><span class="pill"></span><span></span></p><p class="qa-a"></p><p class="qa-meta"></p>`;
  li.querySelector(".pill").className = "pill " + cls;
  li.querySelector(".pill").textContent = label;
  li.querySelector(".qa-q span:last-child").textContent = a.question;
  li.querySelector(".qa-a").textContent = a.text;
  const how = a.decided_by ? "코드가 정한 답" : a.tools.length ? `쓴 도구 ${[...new Set(a.tools)].join(", ")}` : "도구 없음";
  li.querySelector(".qa-meta").textContent =
    `기준일 ${a.asof}, 보낸 매매 ${nSym}종목 ${nRow}줄, ${how}, 모델 호출 ${a.n_llm}번, ${a.seconds}초, 약 ${a.cost_usd}달러`;
  $("qa-log").prepend(li);
  qaState("답이 나왔습니다.");
  $("qa-q").value = "";
}

// ---------- 저장(설계서 6절 4번) ----------

function snapshot() {
  const trades = st.rows.map((r) => Object.fromEntries(Object.entries(r).filter(([, v]) => v !== "" && v !== undefined)));
  return { symbol: st.sym, asof: st.asof, trades };
}

// 서버에 저장합니다. 실패해도 이 화면 안의 기억은 고쳐 두어, 종목을 바꿨다 돌아와도 고친 것이 남습니다.
async function save() {
  clearTimeout(st.saveTimer);
  st.saveTimer = null;
  if (!st.saveOn || st.example) return;
  const snap = snapshot();
  st.saved.symbols[snap.symbol] = { asof: snap.asof, trades: snap.trades };
  st.saved.last = snap.symbol;
  let status = 0, body = null;
  try {
    ({ status, body } = await api("/api/trades", { method: "PUT", body: JSON.stringify(snap) }));
  } catch { /* 서버에 닿지 못함 */ }
  if (status === 200) {
    st.saveErr = null;
    st.savedAt = body.saved_at.slice(11);
  } else {
    st.saveErr = `저장하지 못했습니다(${status ? `${status}${body?.message ? ", " + body.message : ""}` : "서버에 닿지 못함"}). 이 화면을 닫으면 고친 것이 사라집니다`;
  }
  if (snap.symbol === st.sym && st.notes) showNotes();
}

// 창을 닫거나 다른 쪽으로 옮길 때, 아직 보내지 않은 고침을 보냅니다
function flush() {
  if (!st.saveOn || st.example || !st.saveTimer) return;
  clearTimeout(st.saveTimer);
  st.saveTimer = null;
  try {
    fetch("/api/trades", { method: "PUT", keepalive: true, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(snapshot()) });
  } catch { /* 닫히는 중 */ }
}

// ---------- 종목과 기준일 ----------

function load(sym) {
  st.sym = sym;
  st.seq++;
  st.ctl?.abort();
  st.asofErr = null;
  $("asof").removeAttribute("aria-invalid");
  const saved = st.saved.symbols[sym];
  if (saved) {
    st.rows = saved.trades.map((t) => ({ price: "", fx_rate: "", side: "BUY", ...t }));
    st.asof = saved.asof;
    st.example = false;
  } else if (!Object.keys(st.saved.symbols).length && sym === DEFAULT_SYMBOL) {
    st.rows = EXAMPLE.map((t) => ({ price: "", fx_rate: "", ...t }));
    st.example = true;
  } else {
    st.rows = [{ date: "", side: "BUY", qty: "", price: "", fx_rate: "" }];
    st.example = false;
  }
  if (!st.rows.length) st.rows = [{ date: "", side: "BUY", qty: "", price: "", fx_rate: "" }];
  $("sym").value = sym;
  document.dispatchEvent(new CustomEvent("symshown", { detail: sym }));  // 종목 고르기 단추의 이름을 맞춤(ui.js)
  $("asof").value = st.asof;
  renderRows();
  st.res = null;
  showResult("none");
  showRange("none");
  fetchRange();
  run().then(() => { if (st.example) setState($("state").textContent + " 예시 매매예요. 고치면 이 종목의 매매로 저장돼요."); });
}

async function boot() {
  const { status, body } = await api("/api/status");
  if (!body) { $("pipe-line").textContent = `서버 응답을 읽지 못했습니다(${status}).`; return; }
  renderPipe(body);
  if (!body.ok) return;
  const sy = await api("/api/symbols");
  if (sy.status !== 200) { await pipelineFailed(); return; }
  st.calendar = sy.body.calendar;
  st.brokers = sy.body.brokers || [];
  for (const s of sy.body.symbols) st.symbols.set(s.symbol, s);
  const sel = $("sym");
  for (const [label, test] of [["국내", (s) => s.market === "KRX"], ["미국", (s) => s.market !== "KRX"]]) {
    const og = document.createElement("optgroup");
    og.label = label;
    for (const s of sy.body.symbols.filter(test)) {
      const o = document.createElement("option");
      o.value = s.symbol;
      o.textContent = `${s.symbol} ${s.name}`;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  const asof = $("asof");
  asof.min = st.calendar[0]; asof.max = st.calendar[1];
  st.asof = st.calendar[1];
  const tr = await api("/api/trades");
  if (tr.status === 200 && tr.body.enabled) {
    st.saveOn = true;
    st.saved = { symbols: tr.body.symbols || {}, last: tr.body.last };
  } else if (tr.status !== 200) {
    $("ledger-note").textContent = `넣어 둔 매매를 읽지 못해 저장을 껐습니다. ${tr.body?.message || `(${tr.status})`}`;
    $("ledger-note").classList.add("err");
  } else {
    $("ledger-note").textContent = "체결가와 환율을 비우면 그날 종가와 매매기준율로 계산합니다(칸에 흐리게 보이는 값). 평균 단가는 이동평균법이고, 증권사를 고르면 증권사마다 따로 셉니다. 데이터 기간(2년) 전부터 가진 주식은 구분을 「지금 잔고」로 두고 증권사 앱 잔고 화면의 수량, 평균 단가, 평균 매입 환율을 넣으면 됩니다. 이 서버는 매매를 저장하지 않습니다.";
  }
  sel.addEventListener("change", () => {
    // 앞 종목 저장은 기다리지 않습니다(실패해도 종목은 바뀌고, 실패는 상태 줄에 남음)
    if (st.saveTimer) save();
    resetEvents();
    load(sel.value);
  });
  // 기준일이 틀린 동안은 계산하지 않습니다(앞 기준일로 계산한 값이 최신처럼 보이지 않게)
  asof.addEventListener("change", () => {
    if (!asof.value || asof.value < st.calendar[0] || asof.value > st.calendar[1]) {
      st.asofErr = `기준일은 ${st.calendar[0]} ~ ${st.calendar[1]} 안의 날짜로 넣어 주세요`;
      asof.setAttribute("aria-invalid", "true");
      holdRange();
      run();
      return;
    }
    st.asofErr = null;
    asof.removeAttribute("aria-invalid");
    st.asof = asof.value;
    fetchRange();
    changed();
    if (!$("all").hidden) fetchPortfolio();
  });
  window.addEventListener("pagehide", flush);
  // 숨은 탭에서 그린 그림은 폭을 몰라 기본 폭으로 그려지므로, 그림 탭이 보일 때 다시 그림
  document.addEventListener("tabshown", (e) => { if (e.detail === "chart") drawCharts(); if (e.detail === "all") fetchPortfolio(); });
  $("qa-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("qa-q").value); });
  for (const b of document.querySelectorAll(".qa-examples .chip")) {
    b.addEventListener("click", () => { $("qa-q").value = b.dataset.q.replace("이 종목", st.symbols.get(st.sym)?.name || st.sym); $("qa-q").focus(); });
  }
  $("ev-days").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b && !b.disabled) askEvents(b.dataset.d); });
  $("add").addEventListener("click", addRow);
  let rz = null, lastW = window.innerWidth;
  window.addEventListener("resize", () => {
    if (window.innerWidth === lastW) return;
    lastW = window.innerWidth;
    clearTimeout(rz);
    rz = setTimeout(drawCharts, 150);
  });
  $("main").hidden = false;
  const first = st.saved.last && st.symbols.has(st.saved.last) ? st.saved.last : DEFAULT_SYMBOL;
  load(first);
  if (!$("all").hidden) fetchPortfolio();
}

boot();
