"""F 화면(screen/)의 입력 흐름을 헤드리스 Chrome으로 봅니다. 값 대조(check_screen.py)는 다 그린 화면만 보므로, 빠르게
고치거나 서버가 잠깐 닿지 않을 때의 동작은 여기서 봅니다(4단계 독립 검토 2026-11-16의 중간 1 ~ 5번이 다시 생기지 않는지).

  1. 늦게 온 답: 계산 요청을 붙잡아 둔 사이 다른 줄을 틀리게 고치면, 그 뒤 도착한 답이 오류 표시를 지우지 않고 결과는
     「고치기 전 입력」으로 흐리게 남음
  2. 틀린 기준일: 기준일을 지우면 계산 요청을 보내지 않고, 다른 칸을 고쳐도 오류가 남음
  3. 큰 금액: 손익 SQL의 범위를 넘는 금액은 서버가 400으로 돌려주고 화면이 그 줄을 표시함
  4. 넣는 중인 줄만 남음: 수량을 모두 지우면 같은 종목의 앞 결과를 숨기지 않고 흐리게 둠(다듬기 독립 검토
     2026-11-17 중간 1번. 결과 칸이 매매 칸 위라 숨기면 매매 칸이 밀림)
  5. 저장 실패와 종목 바꾸기: 저장 요청이 서버에 닿지 못해도 종목이 바뀌고, 상태 줄에 「저장하지 못했습니다」가 남고,
     잡히지 않은 오류가 없으며, 매매가 없는 종목은 앞 종목의 결과를 숨기고, 돌아오면 고친 값이 남아 있음
  6. 닫을 때 저장: 고친 뒤 0.7초 안에 화면을 떠나도 고친 값이 저장 파일에 들어감

결과 칸은 매매 칸 위(result)와 아래(result-more) 둘이라, 흐림과 숨김은 두 칸을 함께 봅니다(같은 검토 중간 2번).

사용: .venv/bin/python eval/f_screen/check_flows.py [--selftest]
자체 시험은 화면 사본에 고치기 전의 잘못을 하나씩 되살려, 맡은 흐름이 실패로 잡는지 봅니다.
Chrome DevTools 프로토콜(웹소켓)은 표준 라이브러리로 붙습니다. 저장 파일은 git 제외 폴더의 임시 파일이고 끝나면 지웁니다.
"""
import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai import screen_data, screen_server  # noqa: E402
from ai.screen_server import SAFE, serve, stop  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP = SAFE / "f_screen" / "check_flows_trades.json"
START = [{"date": "2025-03-03", "side": "BUY", "qty": "10"}, {"date": "2025-08-01", "side": "BUY", "qty": "5"},
         {"date": "2026-02-02", "side": "SELL", "qty": "8"}]


# ---------- 웹소켓과 DevTools 프로토콜(필요한 만큼만) ----------

class WebSocket:
    def __init__(self, url):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head[:80])

    def send(self, text):
        data, mask = text.encode(), os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("웹소켓이 닫힘")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout):
        """한 메시지(조각을 이어 붙임). timeout 안에 없으면 None."""
        self.sock.settimeout(timeout)
        try:
            if not self.buf:
                self.buf += self.sock.recv(65536)
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(30)
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if b0 & 0x0F == 8:
                raise ConnectionError("웹소켓이 닫힘")
            msg += payload
            if b0 & 0x80:
                return msg.decode()


class Page:
    """페이지 하나. on_pause는 붙잡힌 요청(Fetch.requestPaused)을 받는 함수입니다."""

    def __init__(self, ws):
        self.ws, self.n, self.on_pause, self.errors, self.done = ws, 0, None, [], {}

    def _event(self, m):
        if m.get("method") == "Fetch.requestPaused" and self.on_pause:
            self.on_pause(m["params"])
        elif m.get("method") == "Runtime.exceptionThrown":
            d = m["params"]["exceptionDetails"]
            self.errors.append((d.get("exception") or {}).get("description") or d.get("text"))

    def call(self, method, **params):
        self.n += 1
        my = self.n
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        # 이벤트 처리(on_pause) 안에서 다시 call을 부르면 바깥 call의 답을 먼저 읽을 수 있어, 답은 번호로 모아 둡니다
        while my not in self.done:
            raw = self.ws.recv(30)
            if raw is None:
                raise TimeoutError(method)
            m = json.loads(raw)
            if "id" in m:
                self.done[m["id"]] = m
            else:
                self._event(m)
        m = self.done.pop(my)
        if "error" in m:
            raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result", {})

    def pump(self, seconds):
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            raw = self.ws.recv(left)
            if raw is not None:
                self._event(json.loads(raw))

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("text"))
        return r["result"].get("value")

    def wait(self, expr, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return True
            self.pump(0.1)
        return False


def chrome():
    prof = tempfile.TemporaryDirectory()
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                             f"--user-data-dir={prof.name}", "--remote-debugging-port=0", "--window-size=1280,2000",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    port = None
    end = time.time() + 30
    while time.time() < end and port is None:
        line = proc.stderr.readline()
        m = re.search(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/", line)
        if m:
            port = int(m.group(1))
    if port is None:
        proc.kill()
        raise RuntimeError("Chrome의 DevTools 포트를 찾지 못함")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list") as r:
        target = next(t for t in json.loads(r.read()) if t["type"] == "page")
    return proc, prof, Page(WebSocket(target["webSocketDebuggerUrl"]))


# ---------- 화면 조작 ----------

def type_in(page, el_id, value, event="input"):
    page.js(f"""(() => {{ const e = document.getElementById({json.dumps(el_id)}); e.value = {json.dumps(value)};
        e.dispatchEvent(new Event({json.dumps(event)}, {{ bubbles: true }})); }})()""")


CALC_DONE = "/^계산 \\d/.test(document.getElementById('state').textContent)"
STATE = "document.getElementById('state').textContent"


def state_has(text, err=True):
    cls = " && document.getElementById('state').classList.contains('err')" if err else ""
    return f"document.getElementById('state').textContent.includes({json.dumps(text)}){cls}"


# 결과 칸은 매매 칸 위(result)와 아래(result-more) 둘이라, 둘이 함께 흐리거나 함께 숨어야 합니다
def stale():
    return ("['result', 'result-more'].every(i => document.getElementById(i).classList.contains('stale') "
            "&& !document.getElementById(i).hidden) "
            "&& ['stale-note', 'stale-note-2'].every(i => !document.getElementById(i).hidden)")


HIDDEN = "['result', 'result-more'].every(i => document.getElementById(i).hidden)"


def run_flows(page, port):
    out = {}
    url = f"http://127.0.0.1:{port}/"
    page.call("Runtime.enable")
    page.call("Page.enable")
    page.call("Page.navigate", url=url)
    if not page.wait(CALC_DONE, 20):
        return {"처음 계산": [f"계산을 마치지 못함: {page.js(STATE)!r}"]}

    # 1. 늦게 온 답
    bad, held = [], []
    page.on_pause = held.append
    page.call("Fetch.enable", patterns=[{"urlPattern": "*/api/pnl*", "requestStage": "Request"}])
    type_in(page, "q0", "4")
    end = time.time() + 5
    while not held and time.time() < end:
        page.pump(0.1)
    if not held:
        bad.append("계산 요청을 붙잡지 못함")
    type_in(page, "q1", "abc")
    page.pump(0.6)
    page.on_pause = None
    for h in held:  # 화면이 이미 취소한 요청이면 Chrome이 거절함(그래도 됨)
        try:
            page.call("Fetch.continueRequest", requestId=h["requestId"])
        except RuntimeError:
            pass
    page.pump(1.5)
    if not page.js("document.querySelectorAll('#rows .lrow')[1].classList.contains('badrow')"):
        bad.append("늦게 온 답이 2번 줄의 오류 표시를 지움")
    if not page.js(state_has("2번 줄")):
        bad.append(f"상태 줄에 2번 줄 오류가 없음: {page.js(STATE)!r}")
    if not page.js(stale()):
        bad.append("결과가 흐림(고치기 전 입력)으로 남지 않음")
    if page.js("document.getElementById('q1').getAttribute('aria-invalid')") != "true":
        bad.append("틀린 칸에 aria-invalid가 없음")
    page.call("Fetch.disable")
    out["늦게 온 답이 오류를 지우지 않음"] = bad

    # 2. 틀린 기준일
    bad, sent = [], []
    type_in(page, "q1", "5")
    if not page.wait(CALC_DONE):
        bad.append(f"고친 뒤 다시 계산하지 못함: {page.js(STATE)!r}")
    page.on_pause = lambda p: (sent.append(p["request"]["url"]), page.call("Fetch.continueRequest", requestId=p["requestId"]))
    page.call("Fetch.enable", patterns=[{"urlPattern": "*/api/pnl*", "requestStage": "Request"}])
    asof = page.js("document.getElementById('asof').value")
    type_in(page, "asof", "", "change")
    if not page.wait(state_has("기준일은"), 3):
        bad.append(f"기준일을 지워도 오류가 없음: {page.js(STATE)!r}")
    type_in(page, "q0", "6")
    page.pump(0.8)
    if sent:
        bad.append(f"기준일이 틀린 동안 계산 요청을 {len(sent)}번 보냄")
    if not page.js(state_has("기준일은")) or not page.js(stale()):
        bad.append(f"다른 칸을 고친 뒤 기준일 오류나 흐림이 사라짐: {page.js(STATE)!r}")
    type_in(page, "asof", asof, "change")
    if not page.wait(CALC_DONE):
        bad.append(f"기준일을 되돌려도 계산하지 않음: {page.js(STATE)!r}")
    page.on_pause = None
    page.call("Fetch.disable")
    out["틀린 기준일이면 계산하지 않음"] = bad

    # 3. 큰 금액
    bad = []
    type_in(page, "p1", "7000000")  # 5주 x 700만 달러: 원화로 약 500억 원
    if not page.wait(state_has("2번 줄에서"), 5):
        bad.append(f"큰 금액 오류가 상태 줄에 없음: {page.js(STATE)!r}")
    if not page.js("document.querySelectorAll('#rows .lrow')[1].classList.contains('badrow')"):
        bad.append("큰 금액 줄에 표시가 없음")
    type_in(page, "p1", "")
    if not page.wait(CALC_DONE):
        bad.append(f"체결가를 지워도 다시 계산하지 않음: {page.js(STATE)!r}")
    out["큰 금액은 그 줄을 표시함"] = bad

    # 4. 넣는 중인 줄만 남음: 같은 종목의 앞 결과를 숨기지 않고 흐리게 둠(결과 칸이 매매 칸 위라 숨기면 칸이 밀림)
    bad = []
    keep = {k: page.js(f"document.getElementById('{k}').value") for k in ("q0", "q1", "q2")}
    for k in keep:
        type_in(page, k, "")
    if not page.wait(state_has("날짜와 수량을 넣으면", err=False), 3):
        bad.append(f"넣는 중이라는 안내가 없음: {page.js(STATE)!r}")
    page.pump(0.3)
    if not page.js(stale()):
        bad.append("넣는 중인 줄만 남았는데 앞 결과가 흐림으로 남지 않음(숨거나 그대로)")
    for k, v in keep.items():
        type_in(page, k, v)
    if not page.wait(CALC_DONE):
        bad.append(f"수량을 되돌려도 다시 계산하지 않음: {page.js(STATE)!r}")
    if page.js("['result', 'result-more'].some(i => document.getElementById(i).classList.contains('stale'))"):
        bad.append("다시 계산한 뒤에도 흐림이 남음")
    out["넣는 중인 줄만 남으면 앞 결과를 흐리게 둠"] = bad

    # 5. 저장 실패와 종목 바꾸기
    bad = []
    page.errors.clear()

    def fail_put(p):
        if p["request"]["method"] == "PUT":
            page.call("Fetch.failRequest", requestId=p["requestId"], errorReason="ConnectionRefused")
        else:
            page.call("Fetch.continueRequest", requestId=p["requestId"])
    page.on_pause = fail_put
    page.call("Fetch.enable", patterns=[{"urlPattern": "*/api/trades*", "requestStage": "Request"}])
    type_in(page, "q0", "9")
    if not page.wait(state_has("저장하지 못했습니다"), 4):
        bad.append(f"저장 실패가 상태 줄에 없음: {page.js(STATE)!r}")
    type_in(page, "q0", "12")
    page.pump(0.3)
    type_in(page, "sym", "SPY", "change")
    page.pump(1)
    if page.js("document.getElementById('sym').value") != "SPY":
        bad.append("종목을 바꾸지 못함")
    if not page.js(HIDDEN):
        bad.append("매매가 없는 종목인데 앞 종목의 결과가 보임")
    if page.js("document.getElementById('q0').value") != "":
        bad.append("매매가 없는 종목인데 앞 종목의 줄이 남음")
    type_in(page, "sym", "QQQ", "change")
    if not page.wait(CALC_DONE):
        bad.append(f"돌아온 종목을 계산하지 못함: {page.js(STATE)!r}")
    if page.js("document.getElementById('q0').value") != "12":
        bad.append(f"돌아오니 고친 값이 사라짐: {page.js('document.getElementById(\"q0\").value')!r}")
    if page.errors:
        bad.append(f"잡히지 않은 오류: {page.errors}")
    page.on_pause = None
    page.call("Fetch.disable")
    out["저장 실패에도 종목이 바뀌고 고친 값이 남음"] = bad

    # 6. 닫을 때 저장
    bad = []
    type_in(page, "q0", "11")
    page.call("Page.navigate", url="about:blank")
    page.pump(1.5)
    saved = json.loads(TMP.read_text(encoding="utf-8"))["symbols"]["QQQ"]["trades"][0]["qty"]
    if saved != "11":
        bad.append(f"떠나기 직전 고친 값이 저장되지 않음(저장 파일 {saved!r})")
    out["떠나기 직전 고친 값도 저장"] = bad
    return out


# 자체 시험: 화면 사본에 고치기 전의 잘못을 하나씩 되살려, 맡은 흐름이 실패로 잡는지 봅니다
BUGS = [
    ("늦게 온 답이 오류를 지우지 않음", "일찍 끝나는 갈래에서 앞 요청을 버리지 않음", [
        ("  const my = ++st.seq;\n  st.ctl?.abort();\n  st.ctl = null;\n  const sent", "  const sent"),
        ("  st.ctl = new AbortController();\n  st.notes = null;",
         "  const my = ++st.seq;\n  st.ctl?.abort();\n  st.ctl = new AbortController();\n  st.notes = null;")]),
    ("틀린 기준일이면 계산하지 않음", "틀린 기준일을 계산에서 막지 않음", [
        ("  if (st.asofErr) errs.unshift(st.asofErr);\n", "")]),
    ("큰 금액은 그 줄을 표시함", "서버 400의 줄 번호를 쓰지 않음", [
        ("    if (k !== undefined && map[k] !== undefined) {", "    if (false) {")]),
    ("저장 실패에도 종목이 바뀌고 고친 값이 남음", "저장 실패가 종목 바꾸기를 멈춤", [
        ("""  try {
    ({ status, body } = await api("/api/trades", { method: "PUT", body: JSON.stringify(snap) }));
  } catch { /* 서버에 닿지 못함 */ }""", """  ({ status, body } = await api("/api/trades", { method: "PUT", body: JSON.stringify(snap) }));"""),
        ("""  sel.addEventListener("change", () => {""", """  sel.addEventListener("change", async () => {"""),
        ("    if (st.saveTimer) save();", "    if (st.saveTimer) await save();")]),
    ("저장 실패에도 종목이 바뀌고 고친 값이 남음", "매매가 없는 종목에 앞 결과를 흐리게 남김", [
        ("  showResult(\"none\");\n  run().then", "  run().then"),
        ("      st.res = null;\n      showResult(\"none\");", "      st.res = null;\n      showResult(\"stale\");")]),
    ("넣는 중인 줄만 남으면 앞 결과를 흐리게 둠", "넣는 중인 줄만 남으면 결과를 숨김", [
        ("    if (pending && st.res) {", "    if (false) {")]),
    ("틀린 기준일이면 계산하지 않음", "결과 칸 하나만 흐리게 함", [
        ('  for (const id of ["result", "result-more"]) {', '  for (const id of ["result"]) {')]),
    ("떠나기 직전 고친 값도 저장", "떠날 때 보내지 않음", [
        ("  window.addEventListener(\"pagehide\", flush);\n", "")]),
]


def one_run(static=None):
    """저장 파일을 새로 쓰고 서버와 Chrome을 띄워 흐름 여섯을 돌림. static을 주면 그 폴더의 화면 파일을 씀."""
    TMP.write_text(json.dumps({"symbols": {"QQQ": {"asof": "2026-11-11", "trades": START}}, "last": "QQQ"}),
                   encoding="utf-8")
    old = screen_server.STATIC
    if static:
        screen_server.STATIC = Path(static).resolve()
    httpd, _ = serve(screen_data.OUT, 0, TMP)
    proc = prof = None
    try:
        proc, prof, page = chrome()
        return run_flows(page, httpd.server_address[1])
    finally:
        if proc:
            proc.kill()
            proc.wait()
        if prof:
            prof.cleanup()
        stop(httpd)
        screen_server.STATIC = old
        TMP.unlink(missing_ok=True)


def selftest():
    out = {}
    for flow, name, edits in BUGS:
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(ROOT / "screen", Path(d) / "screen")
            f = Path(d) / "screen" / "app.js"
            src = f.read_text(encoding="utf-8")
            for old, new in edits:
                assert src.count(old) == 1, f"되살리지 못함({name}): {old[:40]!r}"
                src = src.replace(old, new)
            f.write_text(src, encoding="utf-8")
            res = one_run(Path(d) / "screen")
        out[name] = bool(res.get(flow))
        print(f"  {'잡음' if out[name] else '못 잡음'}  {name} → {flow}")
    ok = all(out.values())
    print(f"자체 시험 {sum(out.values())}/{len(out)} 잡음 → {'통과' if ok else '실패'}")
    return 0 if ok else 1


def main():
    if not Path(CHROME).exists():
        print("Chrome이 없어 입력 흐름을 보지 못했습니다(실패로 셈)")
        return 1
    TMP.parent.mkdir(parents=True, exist_ok=True)
    if "--selftest" in sys.argv:
        return selftest()
    res = one_run()
    for name, bad in res.items():
        print(f"  {'통과' if not bad else '실패'}  {name}" + "".join(f"\n      {b}" for b in bad))
    ok = all(not b for b in res.values())
    print(f"입력 흐름 {sum(not b for b in res.values())}/{len(res)} → {'통과' if ok else '실패'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
