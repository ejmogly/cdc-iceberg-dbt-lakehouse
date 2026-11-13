"""관심도 적재기(attention_*.py)가 함께 쓰는 주제 목록 읽기와 attention_daily upsert."""
import csv

from common import ROOT, connect, upsert, utc_now

UPSERT = """
INSERT INTO source.attention_daily AS a
  (topic, obs_date, provider, metric, value, loaded_at)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (topic, obs_date, provider, metric)
DO UPDATE SET
  value     = EXCLUDED.value,
  loaded_at = EXCLUDED.loaded_at
WHERE a.value IS DISTINCT FROM EXCLUDED.value
RETURNING (xmax = 0)
"""


def topics(only=None):
    """loaders/topics.csv. 여러 값은 |로 나눕니다."""
    with open(ROOT / "loaders" / "topics.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for col in ("wiki_articles", "naver_keywords", "related_symbols"):
            r[col] = [v for v in r[col].split("|") if v]
    return [r for r in rows if not only or r["topic_id"] in only]


def load(env, rows):
    """rows: (topic, obs_date, provider, metric, value)."""
    loaded_at = utc_now()
    with connect(env) as conn:
        ins, upd, same = upsert(conn, UPSERT, [r + (loaded_at,) for r in rows])
    print(f"DB: 새로 넣음 {ins}, 고침 {upd}, 그대로 {same}")


def summary(rows):
    by = {}
    for topic, d, provider, metric, _ in rows:
        by.setdefault((topic, metric), []).append(d)
    print("| 주제 | 지표 | 행 | 첫 날 | 끝 날 |")
    print("|---|---|---|---|---|")
    for (topic, metric), ds in by.items():
        print(f"| {topic} | {metric} | {len(ds)} | {min(ds)} | {max(ds)} |")
