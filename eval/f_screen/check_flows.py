"""F 화면(screen/)의 입력 흐름을 헤드리스 Chrome으로 봅니다. 값 대조(check_screen.py)는 다 그린 화면만 보므로, 빠르게
고치거나 서버가 잠깐 닿지 않을 때의 동작은 여기서 봅니다(4단계 독립 검토 2026-11-16의 중간 1 ~ 5번이 다시 생기지 않는지).

  1. 늦게 온 답: 계산 요청을 붙잡아 둔 사이 다른 줄을 틀리게 고치면, 그 뒤 도착한 답이 오류 표시를 지우지 않고 결과는
     「고치기 전 입력」으로 흐리게 남음
  2. 틀린 기준일: 기준일을 지우면 계산 요청을 보내지 않고, 다른 칸을 고쳐도 오류가 남음
  3. 큰 금액: 손익 SQL의 범위를 넘는 금액은 서버가 400으로 돌려주고 화면이 그 줄을 표시함
  4. 넣는 중인 줄만 남음: 수량을 모두 지우면 같은 종목의 앞 결과를 숨기지 않고 흐리게 둠(다듬기 독립 검토
     2026-11-17 중간 1번. 결과 칸이 매매 칸 위라 숨기면 매매 칸이 밀림)
  5. 범위 칸(8번): 앞 종목의 범위 요청을 붙잡아 둔 사이 종목을 되돌려도 늦게 온 답이 지금 범위를 덮지 않고, 기준일이
     틀린 동안 범위가 흐려지고, 범위를 불러오지 못하면 앞 숫자 대신 그렇다고 적음(5단계 독립 검토 2026-11-17 낮음 5번)
  6. 저장 실패와 종목 바꾸기: 저장 요청이 서버에 닿지 못해도 종목이 바뀌고, 상태 줄에 「저장하지 못했습니다」가 남고,
     잡히지 않은 오류가 없으며, 매매가 없는 종목은 앞 종목의 결과를 숨기고, 돌아오면 고친 값이 남아 있음
  7. 묻고 답하기(9번 칸): 빈 질문은 보내지 않고, 보낸 기준일과 넣은 매매가 화면과 같고, 답을 기다리는 동안 묻기 단추가
     막혀 또 보내지 않고, 받은 답이 상태와 쓴 도구와 함께 목록에 들고, 실패(429)는 안내만 남김. 응답을 붙잡거나 꾸며
     넣어 모델을 부르지 않음
  8. 닫을 때 저장: 고친 뒤 0.7초 안에 화면을 떠나도 고친 값이 저장 파일에 들어감

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

    # 2-2. 기준일이 데이터 끝날보다 앞이면 「최신 <끝날>로」 단추(데이터 기간을 늘린 뒤 저장한 종목을 열 때, 11/19)
    bad = []
    end = page.js("document.getElementById('asof').max")
    if page.js("!document.getElementById('asof-latest').hidden !== (document.getElementById('asof').value < "
               f"{json.dumps(end)})"):
        bad.append("최신 단추가 기준일과 맞지 않음(끝날인데 보이거나 앞인데 안 보임)")
    type_in(page, "asof", "2026-11-10", "change")
    page.pump(0.5)
    label = f"최신 {int(end[5:7])}/{int(end[8:])}로"
    if not page.js(f"!document.getElementById('asof-latest').hidden && "
                   f"document.getElementById('asof-latest').textContent === {json.dumps(label)}"):
        bad.append(f"기준일을 앞으로 옮겨도 「{label}」 단추가 없음")
    page.wait(CALC_DONE)
    page.js("document.getElementById('asof-latest').click()")
    if not page.wait(f"document.getElementById('asof').value === {json.dumps(end)}", 3):
        bad.append("단추를 눌러도 기준일이 끝날로 가지 않음")
    if not page.js("document.getElementById('asof-latest').hidden"):
        bad.append("끝날로 간 뒤에도 단추가 남음")
    if not page.wait(CALC_DONE):
        bad.append(f"끝날로 간 뒤 다시 계산하지 않음: {page.js(STATE)!r}")
    out["기준일이 끝날 앞이면 최신 단추"] = bad

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

    # 5. 범위 칸(8번): 늦게 온 답, 틀린 기준일, 불러오기 실패(5단계 독립 검토 2026-11-17 낮음 5번)
    bad, held = [], []
    shown = ("(() => {{ const b = document.getElementById('range-box'); return !b.hidden && !b.classList.contains('stale')"
             " && b.dataset.symbol === '{}'; }})()")
    page.on_pause = held.append
    page.call("Fetch.enable", patterns=[{"urlPattern": "*/api/range*", "requestStage": "Request"}])
    type_in(page, "sym", "SPY", "change")
    end = time.time() + 5
    while not held and time.time() < end:
        page.pump(0.1)
    if not held:
        bad.append("범위 요청을 붙잡지 못함")
    page.on_pause = lambda p: page.call("Fetch.continueRequest", requestId=p["requestId"])
    type_in(page, "sym", "QQQ", "change")
    if not page.wait(shown.format("QQQ"), 5):
        bad.append("돌아온 종목의 범위가 뜨지 않음")
    for h in held:  # 화면이 이미 취소한 요청이면 Chrome이 거절함(그래도 됨)
        try:
            page.call("Fetch.continueRequest", requestId=h["requestId"])
        except RuntimeError:
            pass
    page.pump(1.0)
    if page.js("document.getElementById('range-box').dataset.symbol") != "QQQ":
        bad.append("늦게 온 앞 종목의 범위가 지금 종목의 범위를 덮음")
    asof = page.js("document.getElementById('asof').value")
    type_in(page, "asof", "", "change")
    page.pump(0.3)
    if not page.js("document.getElementById('range-box').classList.contains('stale') && !document.getElementById('rg-stale').hidden"):
        bad.append("기준일이 틀린 동안 범위가 흐려지지 않음")
    type_in(page, "asof", asof, "change")
    if not page.wait(shown.format("QQQ"), 5):
        bad.append("기준일을 되돌려도 범위가 다시 뜨지 않음")
    page.on_pause = lambda p: page.call("Fetch.failRequest", requestId=p["requestId"], errorReason="ConnectionRefused")
    type_in(page, "asof", "2026-06-01", "change")
    if not page.wait("document.getElementById('rg-sub').textContent.includes('범위를 불러오지 못했습니다')", 5):
        bad.append(f"불러오기 실패 문구가 없음: {page.js("document.getElementById('rg-sub').textContent")!r}")
    if page.js("document.getElementById('rg-range').textContent") != "–":
        bad.append("불러오기 실패인데 앞 범위 숫자가 남음")
    page.on_pause = None
    page.call("Fetch.disable")
    type_in(page, "asof", asof, "change")
    if not page.wait(shown.format("QQQ"), 5):
        bad.append("다시 불러온 범위가 뜨지 않음")
    page.wait(CALC_DONE)
    if page.errors:
        bad.append(f"잡히지 않은 오류: {page.errors}")
    out["범위 칸: 늦게 온 답, 틀린 기준일, 불러오기 실패"] = bad

    # 6. 저장 실패와 종목 바꾸기
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

    # 7. 묻고 답하기(9번 칸): 모델 없이 응답을 붙잡거나 꾸며 봄
    bad, held = [], []
    page.errors.clear()
    page.on_pause = held.append
    page.call("Fetch.enable", patterns=[{"urlPattern": "*/api/ask*", "requestStage": "Request"}])
    page.js("document.getElementById('qa-q').value = '  '; document.getElementById('qa-form').requestSubmit()")
    page.pump(0.5)
    if held:
        bad.append("빈 질문을 보냄")
    if not page.js("document.getElementById('qa-state').classList.contains('err')"):
        bad.append("빈 질문에 안내가 없음")
    page.js("document.getElementById('qa-q').value = 'SPY 종가?'; document.getElementById('qa-form').requestSubmit()")
    end = time.time() + 5
    while not held and time.time() < end:
        page.pump(0.1)
    if not held:
        bad.append("질문 요청을 붙잡지 못함")
    else:
        sent = json.loads(held[0]["request"].get("postData") or "{}")
        if sent.get("asof") != page.js("document.getElementById('asof').value") or "QQQ" not in (sent.get("account") or {}):
            bad.append(f"보낸 기준일이나 넣은 매매가 화면과 다름: {list((sent.get('account') or {}))}")
        if not page.js("document.getElementById('qa-go').disabled"):
            bad.append("답을 기다리는 동안 묻기 단추가 막히지 않음")
        page.js("document.getElementById('qa-q').value = '또 물음'; document.getElementById('qa-form').requestSubmit()")
        page.pump(0.5)
        if len(held) > 1:
            bad.append("답을 기다리는 동안 또 보냄")
        # 답 글과 질문은 글자로만 넣음(모델이나 사용자가 쓴 태그가 화면 요소가 되지 않게, 6단계 검토 2026-11-17)
        fake = {"question": "SPY 종가?<img id=\"qa-xss-q\" src=\"x\">", "asof": sent.get("asof"), "status": "answered",
                "text": "가짜 답입니다.<img id=\"qa-xss\" src=\"x\" onerror=\"window.qaXss=1\">",
                "numbers": [], "dates_used": [], "warnings": [], "tools": ["price"], "decided_by": None,
                "rewrite": False, "n_llm": 2, "cost_usd": 0.01, "seconds": 3.2}
        page.call("Fetch.fulfillRequest", requestId=held[0]["requestId"], responseCode=200,
                  responseHeaders=[{"name": "Content-Type", "value": "application/json"}],
                  body=base64.b64encode(json.dumps(fake).encode()).decode())
        shown = "document.querySelector('#qa-log li .qa-a')?.textContent.startsWith('가짜 답입니다.')"
        if not page.wait(shown, 3):
            bad.append("받은 답이 목록에 없음")
        elif page.js("!!document.querySelector('#qa-xss, #qa-xss-q') || !!window.qaXss"):
            bad.append("답이나 질문의 태그가 화면 요소가 됨")
        elif page.js("document.querySelector('#qa-log li .pill').textContent") != "답함" or \
                "쓴 도구 price" not in page.js("document.querySelector('#qa-log li .qa-meta').textContent"):
            bad.append("답의 상태나 쓴 도구 표시가 다름")
        else:
            acct = sent.get("account") or {}
            want = f"보낸 매매 {len(acct)}종목 {sum(len(v) for v in acct.values())}줄"
            if want not in page.js("document.querySelector('#qa-log li .qa-meta').textContent"):
                bad.append(f"답에 보낸 매매가 적히지 않음(기대 「{want}」)")
        if page.js("document.getElementById('qa-go').disabled"):
            bad.append("답을 받은 뒤에도 묻기 단추가 막힘")
    page.on_pause = lambda p: page.call("Fetch.fulfillRequest", requestId=p["requestId"], responseCode=429,
                                        responseHeaders=[{"name": "Content-Type", "value": "application/json"}],
                                        body=base64.b64encode(json.dumps({"error": "BUSY", "message": "앞 질문"}).encode()).decode())
    page.js("document.getElementById('qa-q').value = 'QQQ 종가?'; document.getElementById('qa-form').requestSubmit()")
    if not page.wait("document.getElementById('qa-state').classList.contains('err') && "
                     "document.getElementById('qa-state').textContent.includes('429')", 3):
        bad.append(f"429에 안내가 없음: {page.js("document.getElementById('qa-state').textContent")!r}")
    if page.js("document.querySelectorAll('#qa-log li').length") != 1:
        bad.append("실패한 질문이 답 목록에 들어감")
    page.on_pause = None
    page.call("Fetch.disable")
    if page.errors:
        bad.append(f"잡히지 않은 오류: {page.errors}")
    out["묻고 답하기: 빈 질문, 기다리는 동안, 받은 답, 실패"] = bad

    # 7-2. 그때 무슨 일이(7번 칸): 날짜 단추, 받은 답(태그는 글자로), 늦게 온 답, 실패(2026-11-17)
    bad, held = [], []
    page.errors.clear()
    page.on_pause = held.append
    page.call("Fetch.enable", patterns=[{"urlPattern": "*/api/events*", "requestStage": "Request"}])
    chips = "document.querySelectorAll('#ev-days .chip')"
    fake_ev = lambda d, head: {"symbol": "QQQ", "name": "Q", "date": d, "status": "answered", "head": head,
                               "body": '본문입니다<img id="ev-xss" src="x" onerror="window.evXss=1"> [1]',
                               "move": {"pct": 1.0, "ratio": 2.1, "big": True, "judged": True},
                               "citations": [{"n": 1, "id": "a", "title": '제목<img id="ev-xss2" src="x">', "publisher": "P",
                                              "time": "2026-01-01T10:00:00Z"}],
                               "schedule": [{"id": "fomc-x", "date": d, "kind": "fomc", "title": "FOMC 금리 결정 발표"}],
                               "decided_by": None, "n_llm": 1, "seconds": 3.2, "cost_usd": 0.01, "cached": False,
                               "window_articles": 300,
                               "others": [{"symbol": "SPY", "name": '이름<img id="ev-xss3" src="x">', "pct": -1.5,
                                           "ratio": 2.4, "big": True}]}
    fulfill = lambda p, code, body: page.call("Fetch.fulfillRequest", requestId=p["requestId"], responseCode=code,
                                              responseHeaders=[{"name": "Content-Type", "value": "application/json"}],
                                              body=base64.b64encode(json.dumps(body).encode()).decode())
    if not page.wait(f"{chips}.length > 1", 5):
        bad.append("큰 움직임 날 단추가 없음")
    else:
        d0, d1 = page.js(f"{chips}[0].dataset.d"), page.js(f"{chips}[1].dataset.d")
        page.js(f"{chips}[0].click()")
        end = time.time() + 5
        while not held and time.time() < end:
            page.pump(0.1)
        if not held:
            bad.append("그날 일 요청을 붙잡지 못함")
        else:
            sent = json.loads(held[0]["request"].get("postData") or "{}")
            if sent != {"symbol": "QQQ", "date": d0}:
                bad.append(f"보낸 종목과 날짜가 다름: {sent}")
            if not page.js(f"[...{chips}].every(b => b.disabled)"):
                bad.append("답을 기다리는 동안 날짜 단추가 막히지 않음")
            fulfill(held[0], 200, fake_ev(d0, "머리 문장"))
            if not page.wait("!document.getElementById('ev-card').hidden && document.getElementById('ev-head').textContent === '머리 문장'", 3):
                bad.append("받은 답이 칸에 없음")
            elif page.js("!!document.querySelector('#ev-xss, #ev-xss2, #ev-xss3') || !!window.evXss"):
                bad.append("답, 기사 제목, 다른 자산 이름의 태그가 화면 요소가 됨")
            elif "표준편차 2.1배" not in page.js("document.getElementById('ev-pill').textContent") or \
                    page.js("document.querySelectorAll('#ev-others li').length") != 1:
                bad.append("큰 움직임 표시나 같은 날 다른 자산이 다르게 보임")
            elif page.js("document.querySelectorAll('#ev-cites li').length") != 1 or \
                    "FOMC" not in page.js("document.getElementById('ev-sched').textContent"):
                bad.append("인용 기사나 일정이 다르게 보임")
            if page.js(f"{chips}[0].getAttribute('aria-pressed')") != "true" or page.js(f"{chips}[0].disabled"):
                bad.append("고른 날 표시가 없거나 단추가 막힌 채")
            # 늦게 온 답: 다른 날을 누르고 답이 오기 전에 매매가 있는 다른 종목(GLD)으로 바꾸면 그 답을 버림
            held.clear()
            page.js(f"{chips}[1].click()")
            end = time.time() + 5
            while not held and time.time() < end:
                page.pump(0.1)
            if held:
                if not page.js("document.getElementById('ev-card').hidden"):
                    bad.append("새 날을 누른 뒤에도 앞 날의 답이 보임")
                type_in(page, "sym", "GLD", "change")
                if not page.wait(CALC_DONE):
                    bad.append("GLD를 계산하지 못함")
                fulfill(held[0], 200, fake_ev(d1, "늦은 답"))
                page.pump(0.5)
                if page.js("document.getElementById('ev-head').textContent") == "늦은 답" and \
                        not page.js("document.getElementById('ev-card').hidden"):
                    bad.append("종목을 바꾼 뒤 앞 종목의 늦게 온 답을 보임")
                type_in(page, "sym", "QQQ", "change")
                page.wait(CALC_DONE)
                if not page.js("document.getElementById('ev-card').hidden"):
                    bad.append("종목을 바꿨다 돌아오니 앞 답이 남음")
            else:
                bad.append("둘째 요청을 붙잡지 못함")
            # 가격 그림의 큰 움직임 눈금을 누르면 그날을 물음(눈금끼리 2px 안팎으로 붙은 곳도 있어 눈금 바로 위를 누름)
            held.clear()
            page.wait(f"{chips}.length > 1", 5)
            # 11/18 화면 개편 뒤 그림은 「그림과 그날」 탭에 있어, 탭을 연 뒤(보일 때 그림을 다시 그림) 누름
            page.js("document.getElementById('tab-chart').click()")
            page.pump(0.3)
            tick_d = page.js("document.querySelector('#px line.m-big')?.dataset.d")
            page.js("""(() => { const svg = document.getElementById('px'); svg.scrollIntoView({block: 'center'});
                const r = document.querySelector('#px line.m-big').getBoundingClientRect();
                const o = {clientX: r.left, clientY: r.top - 2, bubbles: true, pointerId: 1, isPrimary: true};
                svg.dispatchEvent(new PointerEvent('pointerdown', o)); svg.dispatchEvent(new MouseEvent('click', o)); })()""")
            end = time.time() + 5
            while not held and time.time() < end:
                page.pump(0.1)
            if not held:
                bad.append("그림의 큰 움직임 눈금을 눌러도 묻지 않음")
            else:
                sent = json.loads(held[0]["request"].get("postData") or "{}")
                if sent.get("date") != tick_d:
                    bad.append(f"눈금의 날({tick_d})이 아닌 날을 물음: {sent.get('date')}")
                fulfill(held[0], 200, fake_ev(tick_d, "그림에서 연 답"))
                page.wait("document.getElementById('ev-head').textContent === '그림에서 연 답'", 3)
            # 실패(429): 안내만 하고 칸은 비운 채
            held.clear()
            page.on_pause = lambda p: fulfill(p, 429, {"error": "BUSY", "message": "앞 날짜를 정리하는 중"})
            page.wait(f"{chips}.length > 1", 5)
            page.js(f"{chips}[0].click()")
            if not page.wait("document.getElementById('ev-state').classList.contains('err') && "
                             "document.getElementById('ev-state').textContent.includes('429')", 3):
                bad.append("429에 안내가 없음: " + repr(page.js("document.getElementById('ev-state').textContent")))
    page.on_pause = None
    page.call("Fetch.disable")
    if page.errors:
        bad.append(f"잡히지 않은 오류: {page.errors}")
    out["그때 무슨 일이: 날짜 단추, 받은 답, 늦은 답, 실패"] = bad

    # 8. 닫을 때 저장
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
    ("기준일이 끝날 앞이면 최신 단추", "기준일을 바꿔도 최신 단추를 다시 보지 않음", [
        ("    st.asof = asof.value;\n    showLatest();\n", "    st.asof = asof.value;\n")]),
    ("큰 금액은 그 줄을 표시함", "서버 400의 줄 번호를 쓰지 않음", [
        ("    if (k !== undefined && map[k] !== undefined) {", "    if (false) {")]),
    ("저장 실패에도 종목이 바뀌고 고친 값이 남음", "저장 실패가 종목 바꾸기를 멈춤", [
        ("""  try {
    ({ status, body } = await api("/api/trades", { method: "PUT", body: JSON.stringify(snap) }));
  } catch { /* 서버에 닿지 못함 */ }""", """  ({ status, body } = await api("/api/trades", { method: "PUT", body: JSON.stringify(snap) }));"""),
        ("""  sel.addEventListener("change", () => {""", """  sel.addEventListener("change", async () => {"""),
        ("    if (st.saveTimer) save();", "    if (st.saveTimer) await save();")]),
    ("저장 실패에도 종목이 바뀌고 고친 값이 남음", "매매가 없는 종목에 앞 결과를 흐리게 남김", [
        ("  showResult(\"none\");\n  showRange(\"none\");", "  showRange(\"none\");"),
        ("      st.res = null;\n      showResult(\"none\");", "      st.res = null;\n      showResult(\"stale\");")]),
    ("넣는 중인 줄만 남으면 앞 결과를 흐리게 둠", "넣는 중인 줄만 남으면 결과를 숨김", [
        ("    if (pending && st.res) {", "    if (false) {")]),
    ("틀린 기준일이면 계산하지 않음", "결과 칸 하나만 흐리게 함", [
        ('  for (const id of ["result", "result-more"]) {', '  for (const id of ["result"]) {')]),
    ("범위 칸: 늦게 온 답, 틀린 기준일, 불러오기 실패", "늦게 온 범위 답을 버리지 않음", [
        ("  st.rctl?.abort();\n  st.rctl = new AbortController();", "  st.rctl = new AbortController();"),
        ("  if (my !== st.rseq) return;\n  if (r.status === 503)", "  if (r.status === 503)")]),
    ("범위 칸: 늦게 온 답, 틀린 기준일, 불러오기 실패", "틀린 기준일에 범위를 흐리게 두지 않음", [
        ("      holdRange();\n", "")]),
    ("범위 칸: 늦게 온 답, 틀린 기준일, 불러오기 실패", "불러오기 실패에 앞 범위를 그대로 둠", [
        ("  if (r.status !== 200) {\n    renderRange(null,", "  if (r.status !== 200 && r.status !== 0) {\n    renderRange(null,")]),
    ("묻고 답하기: 빈 질문, 기다리는 동안, 받은 답, 실패", "답을 기다리는 동안 단추를 막지 않음", [
        ('  $("qa-go").disabled = true;\n', "")]),
    ("묻고 답하기: 빈 질문, 기다리는 동안, 받은 답, 실패", "답 글을 태그로 넣음", [
        ('  li.querySelector(".qa-a").textContent = a.text;', '  li.querySelector(".qa-a").innerHTML = a.text;')]),
    ("묻고 답하기: 빈 질문, 기다리는 동안, 받은 답, 실패", "질문 글을 태그로 넣음", [
        ('  li.querySelector(".qa-q span:last-child").textContent = a.question;',
         '  li.querySelector(".qa-q span:last-child").innerHTML = a.question;')]),
    ("묻고 답하기: 빈 질문, 기다리는 동안, 받은 답, 실패", "빈 질문도 보냄", [
        ('  if (!q) { qaState("질문을 적어 주세요.", true); $("qa-q").focus(); return; }\n', "")]),
    ("그때 무슨 일이: 날짜 단추, 받은 답, 늦은 답, 실패", "7번 칸 답 글을 태그로 넣음", [
        ('  $("ev-body").textContent = a.body || "";', '  $("ev-body").innerHTML = a.body || "";')]),
    ("그때 무슨 일이: 날짜 단추, 받은 답, 늦은 답, 실패", "종목을 바꿔도 오는 중인 답을 버리지 않음", [
        ("    resetEvents();\n    load(sel.value);", "    load(sel.value);"),
        ("  if (my !== st.eseq || !st.res || st.res.symbol !== symbol) return;", "  if (!st.res) return;")]),
    ("그때 무슨 일이: 날짜 단추, 받은 답, 늦은 답, 실패", "그림 눈금을 눌러도 열지 않음", [
        ('  $("px-desc").textContent = drawPrice($("px"), $("px-read"), st.res, rowNo, openEvents);',
         '  $("px-desc").textContent = drawPrice($("px"), $("px-read"), st.res, rowNo);')]),
    ("떠나기 직전 고친 값도 저장", "떠날 때 보내지 않음", [
        ("  window.addEventListener(\"pagehide\", flush);\n", "")]),
]


def one_run(static=None):
    """저장 파일을 새로 쓰고 서버와 Chrome을 띄워 흐름 아홉을 돌림. static을 주면 그 폴더의 화면 파일을 씀."""
    # GLD 한 줄은 7번 칸 흐름이 「매매가 있는 다른 종목」으로 바꿀 때 씀(SPY는 매매가 없는 종목으로 둠)
    TMP.write_text(json.dumps({"symbols": {"QQQ": {"asof": "2026-11-11", "trades": START},
                                           "GLD": {"asof": "2026-11-11", "trades": [{"date": "2025-03-03", "side": "BUY",
                                                                                   "qty": "3"}]}}, "last": "QQQ"}),
                   encoding="utf-8")
    old = screen_server.STATIC
    if static:
        screen_server.STATIC = Path(static).resolve()
    httpd, _ = serve(screen_data.eval_db(), 0, TMP)
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
