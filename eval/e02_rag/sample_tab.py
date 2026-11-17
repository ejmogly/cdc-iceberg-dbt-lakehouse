#!/usr/bin/env python3
"""진행판(진행판.html, git 제외)에 E02 관련 표시 사람 표본 탭을 넣거나 새로 고칩니다(E02 3절 「관련 표시를 만드는 법」 3번).

사용(저장소 루트에서): .venv/bin/python eval/e02_rag/sample_tab.py [--split dev]

- 표본은 개발 풀 전부(80건, 100건보다 적음)입니다. 날짜 순으로 묶고, 날 안에서는 시드로 섞어 어느 판이 고른 기사인지
  드러나지 않게 합니다. 판정 모델의 표시는 보이지 않습니다(사람이 먼저 보고 나중에 맞춰 봄).
- 누른 값은 진행판의 db 컬렉션 e02_labels에 문서 「날짜번호_풀순번」으로 저장됩니다({article, item, label, at}).
  Claude가 ArtifactData로 읽어 eval/e02_rag/kappa.py로 판정 모델과 맞춰 봅니다.
- 기사 제목과 요약이 비공개 진행판에 들어갑니다(2026-11-17 담당자 결정). 저장소에는 이 코드만 둡니다.
- 진행판 안의 <!-- e02-labels:start --> ~ <!-- e02-labels:end --> 블록 셋(스타일, 탭 단추, 탭 내용과 스크립트)만 바꿉니다.
"""
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
BOARD = ROOT / "진행판.html"
SEED = 20261117
NAMES = {"SPY": "SPDR S&P 500", "QQQ": "Invesco QQQ(나스닥 100)", "IWM": "러셀 2000(미국 소형주)",
         "SHY": "1 ~ 3년 미국 국채", "TLT": "20년 이상 미국 국채", "HYG": "하이일드 회사채", "GLD": "금",
         "USO": "원유", "UUP": "미국 달러 강세", "EFA": "선진국(미국 밖) 주식", "EEM": "신흥국 주식",
         "IBIT": "비트코인", "NVDA": "NVIDIA", "AAPL": "Apple", "TSLA": "Tesla", "GOOGL": "Alphabet", "TSM": "TSMC"}
MARKET = {"SPY": "미국 증시 전체", "QQQ": "나스닥과 미국 기술주 전체", "IWM": "미국 소형주", "SHY": "미국 국채 금리와 연준",
          "TLT": "미국 국채 금리와 연준", "HYG": "하이일드 회사채와 신용 스프레드", "GLD": "금 시세", "USO": "원유 시세",
          "UUP": "달러 가치", "EFA": "유럽, 일본 같은 선진국 증시", "EEM": "중국, 인도, 브라질 같은 신흥국 증시",
          "IBIT": "비트코인 시세와 가상자산 시장"}

STYLE = """<style>
.lb-day { display: flex; flex-direction: column; gap: 10px; }
.lb-day h3 { font-size: 17px; font-weight: 800; letter-spacing: -0.02em; }
.lb-day .lb-ctx { font-size: 13px; color: var(--ts-t3); }
.lb-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 14px; align-items: start; padding: 12px 0; border-top: 1px solid var(--line); }
.lb-item:first-of-type { border-top: 0; }
.lb-meta { font-size: 12.5px; color: var(--ts-t3); font-family: var(--mono); }
.lb-title { font-weight: 700; color: var(--ink); line-height: 1.45; }
.lb-sum { font-size: 13.5px; color: var(--ink-2); line-height: 1.5; margin-top: 2px; }
.lb-btns { display: flex; gap: 6px; }
.lb-btn { border: 1px solid var(--line); background: var(--surface); border-radius: 10px; padding: 8px 14px; font-weight: 700; cursor: pointer; color: var(--ink-2); min-width: 64px; }
.lb-btn[aria-pressed="true"].yes { background: var(--ok-soft); color: var(--ok); border-color: var(--ok); }
.lb-btn[aria-pressed="true"].no { background: var(--track); color: var(--ink); border-color: var(--edge); }
.lb-btn:disabled { cursor: default; opacity: 0.6; }
.lb-rule { display: grid; gap: 6px; font-size: 14px; line-height: 1.55; color: var(--ink-2); }
.lb-rule b { color: var(--ink); }
@container (max-width: 560px) { .lb-item { grid-template-columns: minmax(0, 1fr); } }
</style>"""

TAB = ('<button class="tab" role="tab" id="tab-labels" aria-controls="labels" aria-selected="false" tabindex="-1" '
       'data-tab="labels">관련 표시<span class="count" id="labels-count" hidden></span></button>')

PANEL = """<section class="panel x" id="labels" role="tabpanel" aria-labelledby="tab-labels" hidden>
  <section class="x-card" aria-labelledby="s-labels">
    <h2 class="x-display">기사 __N__건에 예, 아니오를 눌러요 <em class="warn">11/23(월) 30분 안</em></h2>
    <div class="x-sec-h"><h3 id="s-labels">E02 관련 표시 사람 표본</h3><span id="lb-progress">불러오는 중</span></div>
    <p class="x-note">큰 움직임이 있던 날마다 검색 판(기준선과 H1)이 고른 기사입니다. 어느 판이 골랐는지와 판정 모델의 표시는 보이지 않습니다. 누르면 이 페이지에 바로 저장되고, 다시 누르면 바뀝니다. 판정 모델과 일치도(카파)가 0.7 이상이어야 판정 모델 점수를 씁니다(E02 3절).</p>
    <div class="lb-rule">
      <p><b>예(관련):</b> 그날의 검색 창(전날 장 마감부터 다음 거래일 장 시작 전까지)에 그 종목이나 그 자산 시장에 실제로 일어난 일을 사실로 전함. 움직임의 원인인지는 따지지 않습니다.</p>
      <p><b>개별 기업:</b> 그 회사의 발표, 실적, 인도량, 소송, 규제, 제품, 거래, 애널리스트 의견 변경, 주가 움직임.</p>
      <p><b>ETF:</b> 그 ETF가 따라가는 시장 전체의 움직임이나 사건. 날마다 머리에 그 시장을 적어 두었습니다.</p>
      <p><b>아니오:</b> 이름만 겹침(Dollar General, 「Gold」 상, 한 종목의 배당 수익률), 한 회사의 보도자료가 그 시장을 말하지 않음(금광 회사의 시추 결과), 그 창의 사실이 없는 일반론과 추천 글, 다른 회사나 다른 자산 이야기. 섞여 있으면 사실 문장이 하나라도 있을 때 예.</p>
      <p class="x-note">제목과 요약만 보고 고릅니다(기사 원문은 열지 않아도 됩니다). 기사 글은 Massive 뉴스이고 재배포하지 않습니다.</p>
    </div>
    <p class="x-callout" id="lb-msg" hidden></p>
  </section>
  <div class="x" id="lb-days"></div>
</section>"""

SCRIPT = r"""<script type="application/json" id="lb-data">__DATA__</script>
<script>
(function () {
  var DATA = JSON.parse(document.getElementById("lb-data").textContent);
  var ALL = []; DATA.forEach(function (d) { d.items.forEach(function (a) { ALL.push(a.key); }); });
  var LB = {}, busy = {}, db = null, canWrite = true, state = "loading";
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function build() {
    var root = $("lb-days");
    DATA.forEach(function (d) {
      var card = el("section", "x-card lb-day");
      var h = el("h3", null, d.date + " " + d.symbol + "(" + d.name + ")");
      card.appendChild(h);
      card.appendChild(el("p", "lb-ctx", (d.market ? "따라가는 시장: " + d.market + ". " : "개별 기업. ") + "창 기사 " + d.window_articles + "건 가운데 " + d.items.length + "건, 창 " + d.window));
      var list = el("div");
      d.items.forEach(function (a) {
        var row = el("div", "lb-item");
        var body = el("div");
        body.appendChild(el("p", "lb-meta", a.time + "  " + a.publisher + (a.symbols ? "  " + a.symbols : "")));
        body.appendChild(el("p", "lb-title", a.title));
        if (a.summary) body.appendChild(el("p", "lb-sum", a.summary));
        var btns = el("div", "lb-btns");
        btns.setAttribute("role", "group"); btns.setAttribute("aria-label", "관련 여부: " + a.title.slice(0, 40));
        [["yes", "예", true], ["no", "아니오", false]].forEach(function (b) {
          var btn = el("button", "lb-btn " + b[0], b[1]); btn.type = "button";
          btn.dataset.key = a.key; btn.dataset.val = b[2] ? "1" : "0"; btn.dataset.article = a.id; btn.dataset.item = d.id;
          btn.setAttribute("aria-pressed", "false");
          btns.appendChild(btn);
        });
        row.appendChild(body); row.appendChild(btns); list.appendChild(row);
      });
      card.appendChild(list); root.appendChild(card);
    });
  }
  function render() {
    document.querySelectorAll(".lb-btn").forEach(function (b) {
      var v = LB[b.dataset.key];
      b.setAttribute("aria-pressed", v && String(v.label ? 1 : 0) === b.dataset.val ? "true" : "false");
      b.disabled = state !== "ok" || !canWrite || !!busy[b.dataset.key];
    });
    var done = ALL.filter(function (k) { return LB[k]; }).length, left = ALL.length - done;
    $("lb-progress").textContent = state === "ok" ? done + "/" + ALL.length + "건 누름" : (state === "loading" ? "불러오는 중" : "저장 불가");
    var c = $("labels-count"); c.hidden = !(state === "ok" && left > 0); c.textContent = left;
    var msg = "";
    if (state === "none") msg = "이 화면에서는 저장할 수 없습니다. claude.ai에 로그인한 상태로 열면 누른 값이 저장됩니다.";
    else if (state === "error") msg = "저장된 값을 읽지 못했습니다. 새로 고침해 주세요.";
    else if (state === "ok" && !canWrite) msg = "보기 권한이라 누를 수 없습니다.";
    else if (state === "ok" && left === 0) msg = "모두 눌렀습니다. Claude에게 「표본 다 했어」라고 알려 주세요.";
    $("lb-msg").hidden = !msg; $("lb-msg").textContent = msg;
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest(".lb-btn"); if (!b || b.disabled || !db) return;
    var key = b.dataset.key, body = { article: b.dataset.article, item: b.dataset.item, label: b.dataset.val === "1", at: new Date().toISOString() };
    busy[key] = true; LB[key] = body; render();
    db.doc("e02_labels/" + key).set(body).then(function () { busy[key] = false; render(); }, function (err) {
      busy[key] = false;
      if (err && (err.code === "invalid_argument" || err.code === "not_granted")) canWrite = false; else console.warn(err);
      render();
    });
  });
  build(); render();
  (async function () {
    var timer = setTimeout(function () { if (state === "loading") { state = "error"; render(); } }, 10000);
    db = window.claude ? await window.claude.use("db") : null;
    if (!db) { clearTimeout(timer); state = "none"; render(); return; }
    var user = await window.claude.use("user");
    if (user) { try { var w = await user.can("data.write"); if (w === false) canWrite = false; } catch (e) {} }
    db.collection("e02_labels").onSnapshot(function (s) {
      clearTimeout(timer);
      var next = {}; s.docs.forEach(function (d) { next[d.id] = d.data(); });
      Object.keys(busy).forEach(function (k) { if (busy[k] && LB[k]) next[k] = LB[k]; });
      LB = next; state = "ok"; render();
    }, function () { clearTimeout(timer); state = "error"; render(); });
  })();
})();
</script>"""


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def block(tag, body):
    return f"<!-- e02-labels:{tag}:start -->\n{body}\n<!-- e02-labels:{tag}:end -->"


def put(html, tag, body, anchor):
    """블록이 있으면 바꾸고, 없으면 anchor 바로 앞에 넣음."""
    rx = re.compile(rf"<!-- e02-labels:{tag}:start -->.*?<!-- e02-labels:{tag}:end -->", re.S)
    if rx.search(html):
        return rx.sub(lambda m: block(tag, body), html, count=1)
    assert html.count(anchor) == 1, f"넣을 자리를 찾지 못함: {anchor[:50]}"
    return html.replace(anchor, block(tag, body) + "\n" + anchor)


def main():
    split = arg("--split", "dev")
    pool = json.loads((HERE / f"pool_{split}.json").read_text(encoding="utf-8"))
    text = json.loads((ROOT / "data" / "generated" / "e02" / f"pool_{split}_text.json").read_text(encoding="utf-8"))
    rng = random.Random(SEED)
    days, n = [], 0
    for it in sorted(pool["items"], key=lambda r: r["date"]):
        by = {a["id"]: a for a in text[it["id"]]}
        items = [{"key": f"{it['id']}_{k}", "id": aid, "time": by[aid]["time"][:16].replace("T", " ") + " UTC",
                  "publisher": by[aid]["publisher"], "symbols": ", ".join(by[aid]["symbols"][:8]),
                  "title": by[aid]["title"], "summary": by[aid]["summary"]} for k, aid in enumerate(it["pool"], 1)]
        rng.shuffle(items)
        n += len(items)
        w = [x[:16].replace("T", " ") for x in it["window"]]
        days.append({"id": it["id"], "date": it["date"], "symbol": it["symbol"], "name": NAMES[it["symbol"]],
                     "market": MARKET.get(it["symbol"], ""), "window_articles": it["window_articles"],
                     "window": f"{w[0]} ~ {w[1]}(미국 동부)", "items": items})
    data = json.dumps(days, ensure_ascii=False).replace("</", "<\\/")
    html = BOARD.read_text(encoding="utf-8")
    html = put(html, "style", STYLE, "</head>") if "</head>" in html else put(html, "style", STYLE, "<header")
    html = put(html, "tab", TAB, '<button class="tab" role="tab" id="tab-decisions"')
    html = put(html, "panel", PANEL.replace("__N__", str(n)) + "\n" + SCRIPT.replace("__DATA__", data),
               "<!-- 결정과 할 일 -->")
    old = 'var TABS = ["summary", "plan", "checks", "decisions", "log"];'
    if old in html:
        html = html.replace(old, 'var TABS = ["summary", "plan", "checks", "labels", "decisions", "log"];')
        html = html.replace("/* 탭: #summary, #plan, #checks, #decisions, #log */",
                            "/* 탭: #summary, #plan, #checks, #labels, #decisions, #log */")
    BOARD.write_text(html, encoding="utf-8")
    print(f"진행판에 표본 탭: {len(days)}일, {n}건")


if __name__ == "__main__":
    main()
