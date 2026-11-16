#!/usr/bin/env python3
"""공개 전 검사. 저장소에 올라갈 파일(git이 추적하거나, 추적 전이지만 .gitignore에 걸리지 않은 파일)을 봅니다.

사용: python3 scripts/check_public.py [--history]

- 가운뎃점: 문서 규칙에서 쓰지 않는 점 네 가지
- 물결표: md 본문에서 글자에 붙은 ~ (범위는 'A ~ B'로 씀. 코드와 주소는 뺌)
- 개인 경로: /Users/이름, /home/이름
- 비밀값: .env의 값이 파일에 들어갔는지(값은 출력하지 않음)
- 금지어: .public-check.local.json(git 제외)의 정규식. 파일이 없으면 건너뜀
- 시세 값: 한 파일에서 날짜 둘레(그 줄과 앞 한 줄, 뒤 두 줄, 앞뒤 300자 안)에 그날의 실제 가격(data/raw/의 시가, 고가, 저가, 종가)이
  나온 짝이 2개를 넘고 날짜가 나온 횟수의 3% 이상이면 찾음(재배포 금지. 큰 파일의 우연한 겹침은 빼려고 비율을 봄).
  날짜는 2025-04-09, 20250409, 2025/04/09, 2025년 4월 9일, 밀리초 시각을 읽음. data/raw/가 없으면 건너뜀
--history면 git 기록 전체(커밋 메시지와 더한 줄)에서 개인 경로, 비밀값, 금지어를 찾습니다.
찾은 것이 있으면 종료 코드 1입니다.
"""
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = {".md", ".py", ".sql", ".sh", ".json", ".yml", ".yaml", ".csv", ".txt", ".svg", ".html",
        ".properties", ".toml", ".example", ""}
DOT_RE = re.compile("[\u00b7\u318d\u2022\u2219]")
PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
CODE_RE = re.compile(r"`[^`]*`")
URL_RE = re.compile(r"https?://\S+")
TILDE_RE = re.compile(r"\S~|~\S")
NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
DATE_RE = re.compile(r"(20\d\d)\s*(?:[-/.]|년\s*)?(\d{1,2})\s*(?:[-/.]|월\s*)?(\d{1,2})일?")
MS_RE = re.compile(r"(?<!\d)1[6-9]\d{11}(?!\d)")
PRICE_LIMIT = 2
PRICE_SHARE = 0.03
NEAR = 300


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8", "replace")


def read_env(path):
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    return env


def secrets():
    """.env에서 자리표시 값과 다르고 8자 이상인 값만 비밀값으로 봅니다."""
    env, example = read_env(ROOT / ".env"), read_env(ROOT / ".env.example")
    return {k: v for k, v in env.items() if len(v) >= 8 and v != example.get(k)}


def forbidden():
    path = ROOT / ".public-check.local.json"
    if not path.exists():
        print("금지어 목록(.public-check.local.json)이 없어 금지어 검사는 건너뜁니다.")
        return []
    rules = json.loads(path.read_text(encoding="utf-8"))["forbidden"]
    return [(label, re.compile(rx)) for label, rx in rules]


def norm(s):
    s = s.replace(",", "")
    return s.rstrip("0").rstrip(".") if "." in s else s


def raw_closes():
    """data/raw/의 실제 가격(시가, 고가, 저가, 종가)을 날짜별로 모읍니다({날짜: {값, ...}}). 원본이 없으면 None(CI 등)."""
    folder = ROOT / "data" / "raw" / "daily_prices"
    if not folder.exists():
        return None
    out = {}
    for path in folder.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            rows = [(datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                     [r.get(k) for k in ("o", "h", "l", "c")]) for r in data.get("results") or []]
        else:
            rows = [(f"{r['basDt'][:4]}-{r['basDt'][4:6]}-{r['basDt'][6:]}",
                     [r.get(k) for k in ("mkp", "hipr", "lopr", "clpr")]) for r in data]
        for d, values in rows:
            for v in values:
                s = norm(str(v)) if v is not None else ""
                if s and distinctive(s):
                    out.setdefault(d, set()).add(s)
    return out


def distinctive(s):
    """평범한 숫자와 겹치지 않을 만한 값만: 소수점이 있는 10 이상의 수, 또는 다섯 자리 이상의 정수."""
    return ("." in s and float(s) >= 10) or (s.isdigit() and len(s) >= 5)


def dates_in(text):
    """글 속 날짜의 (위치, YYYY-MM-DD). 2025-04-09, 20250409, 2025/04/09, 2025년 4월 9일, 밀리초 시각을 읽습니다."""
    for m in DATE_RE.finditer(text):
        y, mo, d = (int(x) for x in m.group(1, 2, 3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            yield m.start(), m.end(), f"{y:04d}-{mo:02d}-{d:02d}"
    for m in MS_RE.finditer(text):
        yield m.start(), m.end(), datetime.fromtimestamp(int(m.group(0)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def scan_prices(closes):
    """날짜 둘레(그 줄과 앞 한 줄, 뒤 두 줄 안, 앞뒤 300자 안)에 그날의 실제 가격이 나온 짝을 셉니다.
    값만 같은 것은 우연히 겹치기 쉬워 세지 않습니다(실제 가격이 수만 개라 가짜 시세와 자주 겹침)."""
    hits = []
    for name, path in published():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        pairs = seen = 0
        for s, e, d in dates_in(text):
            seen += 1
            lo = s
            for _ in range(2):
                lo = text.rfind("\n", 0, max(lo - 1, 0)) + 1 if lo > 0 else 0
            hi = e
            for _ in range(3):
                nxt = text.find("\n", hi + 1)
                hi = len(text) if nxt < 0 else nxt
            near = text[max(lo, s - NEAR):s] + " " + text[e:min(hi, e + NEAR)]
            values = {v for v in (norm(x.group(0)) for x in NUM_RE.finditer(near)) if distinctive(v)}
            pairs += len(closes.get(d, set()) & values)
        if pairs > PRICE_LIMIT and pairs >= PRICE_SHARE * seen:
            hits.append((name, 0, "시세 값", f"날짜와 그날 실제 가격의 짝 {pairs}개, 날짜 {seen}번 가운데(값은 출력하지 않음)"))
    return hits


def check_line(line, words, keys):
    found = []
    if PATH_RE.search(line):
        found.append(("개인 경로", "/Users 또는 /home 아래 이름"))
    found += [("비밀값", f".env의 {k}") for k, v in keys.items() if v in line]
    found += [("금지어", label) for label, rx in words if rx.search(line)]
    return found


def published():
    for name in git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0"):
        path = ROOT / name
        if name and path.is_file() and (path.suffix in TEXT or path.name in {"Makefile", "Dockerfile"}):
            yield name, path


def scan_files(words, keys):
    hits = []
    for name, path in published():
        hits += [(name, 0, kind, label) for kind, label in check_line(name, words, keys)]
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        in_code = False
        for no, line in enumerate(text.splitlines(), 1):
            hits += [(name, no, kind, label) for kind, label in check_line(line, words, keys)]
            if DOT_RE.search(line):
                hits.append((name, no, "가운뎃점", "쉼표나 '와/과'로"))
            if path.suffix != ".md":
                continue
            if FENCE_RE.match(line):
                in_code = not in_code
                continue
            if not in_code and TILDE_RE.search(URL_RE.sub("", CODE_RE.sub("", line))):
                hits.append((name, no, "물결표", "글자에 붙은 ~, 범위는 'A ~ B'"))
    return hits


def scan_history(words, keys):
    hits = Counter()
    for commit in git("rev-list", "--all").split():
        for line in git("show", "--no-color", "--format=%B", commit).splitlines():
            if line.startswith("+++") or line.startswith("-") or line.startswith(" ") or line.startswith("@@"):
                continue
            for kind, label in check_line(line, words, keys):
                hits[(commit[:7], kind, label)] += 1
    return [(commit, count, kind, label) for (commit, kind, label), count in hits.items()]


def main():
    words, keys = forbidden(), secrets()
    if "--history" in sys.argv:
        hits = scan_history(words, keys)
        for commit, count, kind, label in hits:
            print(f"커밋 {commit}  {kind}  {label}  {count}줄")
    else:
        hits = scan_files(words, keys)
        closes = raw_closes()
        if closes is None:
            print("data/raw/가 없어 시세 값 검사는 건너뜁니다.")
        else:
            hits += scan_prices(closes)
        for name, no, kind, label in hits:
            print(f"{name}:{no}  {kind}  {label}")
    print(f"\n찾은 것 {len(hits)}건" if hits else "찾은 것 없음")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
