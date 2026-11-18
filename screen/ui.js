// 탭과 시트(11/18 화면 개편). 원본은 3판 뼈대(담당자가 쓰는 문서 페이지 모양 기준) 시작 틀의 스크립트, 탭이 보일 때 tabshown 사건을 더함
// 탭(주소 #탭 id로 바로 열림), 「지난 기록」 목록, 시트.
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab[data-tab]"));
  var pastBtn = document.getElementById("past-btn"), pastList = document.getElementById("past-list"), pastLbl = pastBtn && pastBtn.querySelector(".lbl");
  function setPast(open) { if (!pastBtn || !pastList) return; pastList.hidden = !open; pastBtn.setAttribute("aria-expanded", String(open)); }
  function show(id, focus) {
    tabs.forEach(function (t) {
      var on = t.getAttribute("data-tab") === id, p = document.getElementById(t.getAttribute("data-tab"));
      t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1;
      if (p) p.hidden = !on;
      if (on && p) document.dispatchEvent(new CustomEvent("tabshown", { detail: id }));
      if (on && focus) t.focus();
      if (on && pastBtn) {
        var inPast = !!(pastList && pastList.contains(t));
        pastBtn.classList.toggle("on", inPast);
        if (pastLbl) {
          pastLbl.textContent = "지난 기록";
          if (inPast) { var nm = document.createElement("span"); nm.className = "pn"; nm.textContent = ": " + t.firstElementChild.textContent.trim(); pastLbl.appendChild(nm); }
        }
      }
    });
  }
  tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      show(t.getAttribute("data-tab"));
      if (pastList && pastList.contains(t)) setPast(false);
      try { history.replaceState(null, "", "#" + t.getAttribute("data-tab")); } catch (e) {}
    });
    t.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      var vis = tabs.filter(function (x) { return x.offsetParent !== null; }), i = vis.indexOf(t);
      if (i < 0) return;
      show(vis[(i + (e.key === "ArrowRight" ? 1 : -1) + vis.length) % vis.length].getAttribute("data-tab"), true);
    });
  });
  if (pastBtn) pastBtn.addEventListener("click", function () { setPast(pastList.hidden); });
  var h = (location.hash || "").replace("#", ""), target = h && document.getElementById(h);
  if (target && target.getAttribute("role") === "tabpanel") show(h);

  var wrap = document.getElementById("x-sheet"), shT = document.getElementById("x-sheet-t"), shS = document.getElementById("x-sheet-s"), shB = document.getElementById("x-sheet-b"), last = null;
  function openSheet(id, from) {
    var tpl = document.getElementById(id);
    if (!tpl || !wrap) return;
    shT.textContent = tpl.getAttribute("data-title") || "";
    var sub = tpl.getAttribute("data-sub") || ""; shS.textContent = sub; shS.hidden = !sub;
    shB.innerHTML = ""; shB.appendChild(tpl.content.cloneNode(true));
    last = from; wrap.hidden = false; document.body.style.overflow = "hidden";
    wrap.querySelector(".x-close").focus();
  }
  function closeSheet() { if (!wrap || wrap.hidden) return; wrap.hidden = true; document.body.style.overflow = ""; if (last) last.focus(); }
  document.addEventListener("click", function (e) {
    var o = e.target.closest("[data-sheet]"); if (o) { openSheet(o.getAttribute("data-sheet"), o); return; }
    var g = e.target.closest("[data-goto]");
    if (g) { var t = document.querySelector('.tab[data-tab="' + g.getAttribute("data-goto") + '"]'); if (t) t.click(); return; }
    if (e.target.closest("[data-close]")) { closeSheet(); return; }
    if (pastList && !pastList.hidden && !e.target.closest("#past-list, #past-btn")) setPast(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (wrap && !wrap.hidden) { closeSheet(); return; }
    if (pastList && !pastList.hidden) { setPast(false); pastBtn.focus(); }
  });
})();

// 종목 고르기 창(11/18 담당자 「종목을 눌렀을 때 뜨는 화면이 너무 구려」). 숨긴 select(#sym)의 묶음과 항목으로 목록을 만들고,
// 고르면 select 값을 바꾸고 change 사건을 보내 app.js가 그대로 처리합니다.
(function () {
  var sel = document.getElementById("sym"), btn = document.getElementById("sym-btn"), wrap = document.getElementById("sym-pick");
  var q = document.getElementById("pick-q"), list = document.getElementById("pick-list"), empty = document.getElementById("pick-empty");
  if (!sel || !btn || !wrap) return;
  function split(o) { var t = o.textContent.trim(), i = t.indexOf(" "); return { code: o.value, name: i < 0 ? t : t.slice(i + 1) }; }
  function label() {
    var o = sel.options[sel.selectedIndex];
    if (!o) return;
    var it = split(o);
    document.getElementById("sym-name").textContent = it.name;
    document.getElementById("sym-code").textContent = it.code;
    btn.setAttribute("aria-label", "종목 " + it.name + " " + it.code + ", 바꾸기");
  }
  function items() { return Array.prototype.slice.call(list.querySelectorAll(".pick-item")); }
  function visible() { return items().filter(function (b) { return !b.hidden; }); }
  function build() {
    list.innerHTML = "";
    document.getElementById("pick-s").textContent = Array.prototype.map.call(sel.querySelectorAll("optgroup"), function (g) {
      return g.label + " " + g.querySelectorAll("option").length + "종목"; }).join(", ");
    Array.prototype.forEach.call(sel.querySelectorAll("optgroup"), function (g) {
      var box = document.createElement("div"); box.className = "pick-group"; box.setAttribute("role", "group");
      var h = document.createElement("p"); h.className = "pick-h"; h.textContent = g.label; h.id = "pick-g-" + g.label;
      box.setAttribute("aria-labelledby", h.id); box.appendChild(h);
      Array.prototype.forEach.call(g.querySelectorAll("option"), function (o) {
        var it = split(o), b = document.createElement("button");
        b.type = "button"; b.className = "pick-item"; b.dataset.code = it.code; b.setAttribute("role", "option");
        var on = o.value === sel.value; b.setAttribute("aria-selected", String(on));
        var n = document.createElement("span"); n.className = "pick-name"; n.textContent = it.name;
        var c = document.createElement("span"); c.className = "pick-code"; c.textContent = it.code;
        var m = document.createElement("span"); m.className = "pick-mark"; m.setAttribute("aria-hidden", "true");
        b.append(n, c, m);
        b.dataset.find = (it.code + " " + it.name).toLowerCase();
        box.appendChild(b);
      });
      list.appendChild(box);
    });
  }
  function filter() {
    var v = q.value.trim().toLowerCase(), any = false;
    Array.prototype.forEach.call(list.querySelectorAll(".pick-group"), function (g) {
      var shown = 0;
      Array.prototype.forEach.call(g.querySelectorAll(".pick-item"), function (b) { b.hidden = !!v && b.dataset.find.indexOf(v) < 0; if (!b.hidden) shown++; });
      g.hidden = !shown; any = any || shown > 0;
    });
    empty.hidden = any;
  }
  function open() {
    build(); q.value = ""; filter();
    wrap.hidden = false; btn.setAttribute("aria-expanded", "true"); document.body.style.overflow = "hidden";
    var cur = list.querySelector('.pick-item[aria-selected="true"]');
    if (cur) cur.scrollIntoView({ block: "center" });
    q.focus();
  }
  function close(back) {
    if (wrap.hidden) return;
    wrap.hidden = true; btn.setAttribute("aria-expanded", "false"); document.body.style.overflow = "";
    if (back !== false) btn.focus();
  }
  function pick(code) {
    close();
    if (code === sel.value) return;
    sel.value = code;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    label();
  }
  btn.addEventListener("click", open);
  sel.addEventListener("change", label);
  document.addEventListener("symshown", label);
  q.addEventListener("input", filter);
  wrap.addEventListener("click", function (e) {
    var b = e.target.closest(".pick-item"); if (b) { pick(b.dataset.code); return; }
    if (e.target.closest("[data-pick-close]")) close();
  });
  wrap.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.stopPropagation(); close(); return; }
    var vis = visible(), i = vis.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!vis.length) return;
      var d = e.key === "ArrowDown" ? 1 : -1;
      var next = i < 0 ? (d > 0 ? 0 : vis.length - 1) : i + d;
      if (next < 0) { q.focus(); return; }
      vis[Math.min(next, vis.length - 1)].focus();
    } else if (e.key === "Enter" && document.activeElement === q && vis.length) {
      e.preventDefault(); pick(vis[0].dataset.code);
    }
  });
  label();
})();
