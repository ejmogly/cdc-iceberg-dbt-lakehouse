"""적재기들이 함께 쓰는 것: .env 읽기, HTTP 호출, 받은 원본 저장, DB 연결과 upsert.

- HTTP는 운영체제의 인증서 저장소로 TLS를 검사합니다(truststore). 검사를 끄지 않습니다.
- 키와 연락처 같은 비밀값은 출력하지 않고, 오류 글에 섞이면 가립니다.
- 받은 원본은 data/raw/(git 제외)에만 둡니다. 시세, 뉴스, 관심도 값은 재배포하지 않습니다.
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import truststore

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
USER_AGENT = "cdc-iceberg-dbt-lakehouse-loader/0.1"
_TLS = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def load_env():
    """저장소 .env를 읽습니다. 같은 이름의 환경 변수가 있으면 그 값이 먼저입니다. 값은 출력하지 않습니다."""
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip("'\"")
    for k, v in os.environ.items():
        if v:
            env[k] = v
    return env


def need(env, name):
    value = env.get(name, "")
    if not value:
        sys.exit(f".env에 {name} 값이 없습니다(.env.example 참고)")
    return value


class FetchError(Exception):
    pass


class Http:
    """비밀값을 가리고, 호출 간격을 지키고, 429면 기다렸다 다시 하는 HTTP 도구."""

    def __init__(self, secrets=(), min_interval=0.0, wait_429=60, tries_429=3, timeout=90, user_agent=USER_AGENT):
        self.secrets = [s for s in secrets if s]
        self.min_interval = min_interval
        self.wait_429 = wait_429
        self.tries_429 = tries_429
        self.timeout = timeout
        self.user_agent = user_agent
        self.calls = 0
        self._last = 0.0

    def mask(self, text):
        for s in self.secrets:
            for form in {s, urllib.parse.quote(s, safe=""), urllib.parse.quote_plus(s)}:
                text = text.replace(form, "***")
        return text

    def _pace(self):
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()

    def request(self, url, headers=None, data=None, method=None):
        """(HTTP 상태, 본문 bytes). 429는 wait_429초, 그다음은 두 배씩 기다리며 tries_429번까지 다시 합니다."""
        wait = self.wait_429
        for attempt in range(self.tries_429 + 1):
            self._pace()
            self.calls += 1
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={"User-Agent": self.user_agent, **(headers or {})})
            try:
                with urllib.request.urlopen(req, context=_TLS, timeout=self.timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                body = e.read()
                if e.code == 429 and attempt < self.tries_429:
                    print(f"  429(호출 제한). {wait}초 기다렸다 다시 합니다", flush=True)
                    time.sleep(wait)
                    wait *= 2
                    continue
                return e.code, body
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as e:
                raise FetchError(self.mask(f"{type(e).__name__}: {getattr(e, 'reason', e)}")) from None
        return 429, b""

    def json(self, url, headers=None, data=None, method=None):
        status, body = self.request(url, headers, data, method)
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        return status, parsed, body

    def describe(self, status, body):
        """실패 사유를 짧게. 비밀값은 가립니다."""
        text = body[:300].decode("utf-8", "replace") if body else ""
        return self.mask(f"HTTP {status} {' '.join(text.split())}")


def save_raw(source, name, payload):
    """data/raw/{source}/{name}.json에 받은 원본을 둡니다."""
    folder = RAW / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def read_raw(source, name):
    path = RAW / source / f"{name}.json"
    if not path.exists():
        sys.exit(f"{path.relative_to(ROOT)}가 없습니다. --from-raw 없이 먼저 받으세요")
    return json.loads(path.read_text(encoding="utf-8"))


def date_args(parser, default_days):
    parser.add_argument("--start", type=date.fromisoformat, help="시작일 YYYY-MM-DD(그날 포함)")
    parser.add_argument("--end", type=date.fromisoformat, help="끝일 YYYY-MM-DD(그날 포함). 기본은 오늘")
    parser.add_argument("--days", type=int, default=default_days,
                        help=f"--start가 없을 때 끝일부터 거꾸로 며칠(기본 {default_days}). 처음 넣을 때는 365")
    parser.add_argument("--dry-run", action="store_true", help="받아서 원본만 저장하고 DB에는 쓰지 않음")
    parser.add_argument("--from-raw", action="store_true", help="API를 부르지 않고 저장된 원본으로 적재")


def resolve_dates(args, today=None):
    end = args.end or today or date.today()
    start = args.start or (end - timedelta(days=args.days - 1))
    if start > end:
        sys.exit("시작일이 끝일보다 늦습니다")
    return start, end


def new_parser(doc):
    return argparse.ArgumentParser(description=doc.strip().splitlines()[0],
                                   formatter_class=argparse.RawDescriptionHelpFormatter, epilog=doc)


def utc_now():
    return datetime.now(timezone.utc)


def connect(env):
    """원천 PostgreSQL에 붙습니다. 접속값은 .env(POSTGRES_*)에서 읽고, 같은 이름의 환경 변수가 먼저입니다."""
    import psycopg
    return psycopg.connect(host=env.get("POSTGRES_HOST", "localhost"), port=int(env.get("POSTGRES_PORT", "5432")),
                           dbname=need(env, "POSTGRES_DB"), user=need(env, "POSTGRES_USER"),
                           password=need(env, "POSTGRES_PASSWORD"), connect_timeout=10)


def upsert(conn, sql, rows):
    """RETURNING (xmax = 0)을 붙인 upsert를 행마다 돌려 (새로 넣음, 고침, 그대로)를 셉니다.

    값이 같은 행은 WHERE ... IS DISTINCT FROM에 걸려 고치지 않으므로 RETURNING에도 나오지 않습니다.
    고치지 않은 행은 WAL에 남지 않아 CDC 이벤트도 생기지 않습니다.
    """
    inserted = updated = 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows, returning=True)
        while True:
            row = cur.fetchone()
            if row is not None:
                if row[0]:
                    inserted += 1
                else:
                    updated += 1
            if not cur.nextset():
                break
    conn.commit()
    return inserted, updated, len(rows) - inserted - updated


def report(title, lines):
    print(f"\n{title}")
    for line in lines:
        print(f"  {line}")
