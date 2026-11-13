"""0단계 조사 스크립트가 함께 쓰는 키 읽기와 내려받기.

키는 저장소의 .env에서 읽는다. curl 설정을 표준 입력으로 넘겨 명령줄, 출력, 오류 글에 키가 남지 않게 한다.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_key(name):
    """값이 없으면 빈 글자. 값을 출력하지 않는다."""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1) == name:
            return m.group(2).strip().strip("'\"")
    return ""


def get_json(url, headers=()):
    """(HTTP 상태, 해석한 JSON 또는 None)을 돌려준다."""
    cfg = [f'url = "{url}"', "silent", "max-time = 90", 'write-out = "\\n%{http_code}"']
    cfg += [f'header = "{h}"' for h in headers]
    p = subprocess.run(["curl", "--config", "-"], input="\n".join(cfg), capture_output=True, text=True)
    body, _, code = p.stdout.rpartition("\n")
    try:
        return int(code), json.loads(body)
    except ValueError:
        return int(code) if code.isdigit() else 0, None
