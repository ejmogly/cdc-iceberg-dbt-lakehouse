#!/usr/bin/env python3
"""Silver 현재 상태 다섯 테이블을 Bronze의 새 스냅샷만 읽어 갱신합니다(증분 읽기, docs/decisions/0011).

규모 시험(docs/reports/2026-11-18_scale-test.md)에서 Silver MERGE가 바뀐 것이 없어도 Bronze 이력 전체를 펼쳐,
이력이 쌓일수록 느려졌습니다(8.7초 → 73초). 증분 Gold(sql/gold_incremental/run.py)와 같은 뼈대로 바꿉니다.

테이블마다:
  1. 구간 고정: progress에 저장한 Bronze 스냅샷이 시작(빼고), 지금 main의 스냅샷이 끝(넣고)입니다.
     같으면 할 일이 없습니다.
  2. 입력: 구간에 더해진 Bronze 행만(create_changelog_view의 INSERT). Bronze는 덧붙이기만 하고,
     압축(replace) 스냅샷은 변경 뷰가 건너뛰므로 압축한 파일을 다시 읽지 않습니다.
     progress가 없거나(첫 실행), 시작 스냅샷이 만료됐거나, --full이면 끝 스냅샷의 Bronze 전체를 읽습니다.
  3. 검사와 최신 후보, MERGE: sql/silver/<테이블>/1_incoming.sql과 2_merge.sql을 그대로 씁니다.
     1_incoming.sql의 `lake.bronze.<테이블>` 한 곳만 입력 뷰 이름으로 바꿔 끼웁니다(한 곳이 아니면 멈춤).
  4. progress 저장: 그 테이블의 끝 스냅샷. MERGE 뒤 저장 전에 멈추면 다음 실행이 같은 구간을 다시 읽고,
     MERGE의 버전 가드(LSN, offset)가 이미 반영한 변경을 거절해 답이 같습니다(docs/decisions/0010).

증분에서는 1_incoming.sql의 검사(봉투, 같은 전달 위치의 다른 원문, 빈 값)가 구간 안의 행에만 걸립니다.
구간을 넘는 검사는 --full이나 --compare로 봅니다.

  --compare  끝난 뒤 Bronze 전체로 만든 최신 후보(전체 재계산의 답)와 Silver를 모든 칸으로 비교합니다.
             (Silver에만, 전체 재계산에만) 행 수를 찍고, 0 / 0이 아니면 1로 끝납니다. 이력 전체를 읽으므로 느립니다.
  --fail-after-merge 시험용: 첫 테이블의 MERGE 뒤 progress 저장 전에 멈춥니다(종료코드 3).

Airflow 컨테이너(/opt/dbt-venv)나 dbt 컨테이너에서 돌립니다(pyhive, sqlparse).
  docker compose --profile spark run --rm --entrypoint python dbt \\
    /workspace/sql/silver/run.py --compare
"""
import argparse
import os
import sys
import time
from pathlib import Path

import sqlparse
from pyhive import hive

HERE = Path(__file__).resolve().parent
TABLES = ["orders", "executions", "holdings", "daily_prices", "fx_rates"]
PROGRESS = "lake.silver.merge_progress"
# Silver 테이블의 칸(= incoming_<테이블> 뷰의 칸, 2_merge.sql이 INSERT *, UPDATE SET *)


class Spark:
    def __init__(self):
        self.conn = hive.connect(
            host=os.environ.get("SPARK_THRIFT_HOST", "spark"),
            port=int(os.environ.get("SPARK_THRIFT_PORT", "10000")),
            username="silver", auth="NOSASL")
        self.cur = self.conn.cursor()

    def run(self, sql):
        self.cur.execute(sql)

    def rows(self, sql):
        self.cur.execute(sql)
        return self.cur.fetchall()

    def one(self, sql):
        r = self.rows(sql)
        return r[0][0] if r else None

    def script(self, text):
        n = 0
        for stmt in sqlparse.split(text):
            body = sqlparse.format(stmt, strip_comments=True).strip().rstrip(";").strip()
            if body:
                self.cur.execute(body)
                n += 1
        return n


def log(msg):
    print(msg, flush=True)


def incoming_sql(table, view):
    """1_incoming.sql에서 Bronze 테이블 한 곳을 입력 뷰로 바꿉니다."""
    text = (HERE / table / "1_incoming.sql").read_text(encoding="utf-8")
    ref = f"lake.bronze.{table}"
    if text.count(ref) != 1:
        raise SystemExit(f"{table}/1_incoming.sql에 {ref}가 한 곳이 아닙니다({text.count(ref)}곳). 바꿔 끼울 수 없습니다")
    return text.replace(ref, view)


def progress(spark):
    spark.run(f"""CREATE TABLE IF NOT EXISTS {PROGRESS} (
        bronze_table STRING, snapshot_id BIGINT, mode STRING, rows_in BIGINT, done_at TIMESTAMP
      ) USING iceberg TBLPROPERTIES ('format-version' = '2')""")
    return {r[0]: (r[1], r[2], r[3]) for r in spark.rows(f"SELECT bronze_table, snapshot_id, mode, rows_in FROM {PROGRESS}")}


def save_progress(spark, prog):
    values = ", ".join(f"('{t}', {s}, '{m}', {n}, current_timestamp())" for t, (s, m, n) in sorted(prog.items()))
    spark.run(f"INSERT OVERWRITE TABLE {PROGRESS} VALUES {values}")


def define_input(spark, table, start, end, full):
    """입력 뷰 silver_input_<테이블>을 만들고 (모드, 읽은 행 수)를 돌려줍니다."""
    view = f"silver_input_{table}"
    cols = "payload, topic, partition_id, kafka_offset, kafka_ts, ingested_at"
    if not full and start is not None and not spark.one(
            f"SELECT count(*) FROM lake.bronze.{table}.snapshots WHERE snapshot_id = {start}"):
        log(f"  {table}: progress 스냅샷 {start}가 만료돼 없어 전체를 읽습니다")
        full = True
    if full or start is None:
        spark.run(f"CREATE OR REPLACE TEMPORARY VIEW {view} AS "
                  f"SELECT {cols} FROM lake.bronze.{table} VERSION AS OF {end}")
        mode = "full"
    else:
        spark.run(f"""CALL lake.system.create_changelog_view(
            table => 'bronze.{table}',
            options => map('start-snapshot-id', '{start}', 'end-snapshot-id', '{end}'),
            changelog_view => 'bronze_changes_{table}')""")
        # Bronze는 덧붙이기만 합니다. INSERT가 아닌 변경이 보이면 누가 Bronze를 고친 것이라 멈춥니다
        other = spark.one(f"SELECT count(*) FROM bronze_changes_{table} WHERE _change_type <> 'INSERT'")
        if other:
            raise SystemExit(f"{table}: Bronze 구간에 INSERT가 아닌 변경이 {other}행 있습니다(--full로 다시)")
        spark.run(f"CREATE OR REPLACE TEMPORARY VIEW {view} AS SELECT {cols} FROM bronze_changes_{table}")
        mode = "incremental"
    return view, mode, int(spark.one(f"SELECT count(*) FROM {view}"))


def compare(spark, table, end):
    """Bronze 전체(끝 스냅샷)로 만든 최신 후보와 Silver를 모든 칸으로 비교. (Silver에만, 전체에만)"""
    spark.run(f"CREATE OR REPLACE TEMPORARY VIEW silver_check_{table} AS "
              f"SELECT payload, topic, partition_id, kafka_offset, kafka_ts, ingested_at "
              f"FROM lake.bronze.{table} VERSION AS OF {end}")
    spark.script(incoming_sql(table, f"silver_check_{table}"))
    a = spark.one(f"SELECT count(*) FROM (SELECT * FROM lake.silver.{table} EXCEPT SELECT * FROM incoming_{table})")
    b = spark.one(f"SELECT count(*) FROM (SELECT * FROM incoming_{table} EXCEPT SELECT * FROM lake.silver.{table})")
    return int(a), int(b)


def main():
    p = argparse.ArgumentParser(description="Silver 증분 갱신")
    p.add_argument("tables", nargs="*", help=f"고른 테이블만(기본 {', '.join(TABLES)})")
    p.add_argument("--full", action="store_true", help="Bronze 전체를 읽음(비상 경로)")
    p.add_argument("--compare", action="store_true", help="끝난 뒤 전체 재계산의 답과 비교, 다르면 1로 끝남")
    p.add_argument("--fail-after-merge", action="store_true", help="시험용: 첫 MERGE 뒤 progress 저장 전에 멈춤")
    a = p.parse_args()
    tables = a.tables or TABLES
    bad = [t for t in tables if t not in TABLES]
    if bad:
        sys.exit(f"모르는 테이블: {bad}")

    spark = Spark()
    prog = progress(spark)
    ends = {}
    for t in tables:
        t0 = time.time()
        start = prog.get(t, (None,))[0]
        end = spark.one(f"SELECT snapshot_id FROM lake.bronze.{t}.refs WHERE name = 'main'")
        ends[t] = end
        if end is None:
            log(f"{t}: Bronze 스냅샷이 없어 건너뜀")
            continue
        if start == end and not a.full:
            log(f"{t}: 새 Bronze 스냅샷이 없어 할 일이 없습니다({round(time.time() - t0, 1)}초)")
            continue
        view, mode, n = define_input(spark, t, start, end, a.full)
        if mode == "incremental" and n == 0:
            # Flink는 행 없는 checkpoint도 빈 스냅샷으로 남깁니다. 새 행이 없으면 MERGE를 건너뛰어 Silver에
            # 빈 스냅샷을 만들지 않고(증분 Gold가 할 일 없음으로 넘어가게), progress만 앞으로 옮깁니다
            prog[t] = (end, mode, 0)
            save_progress(spark, prog)
            log(f"{t}: 구간 {start} → {end}에 새 Bronze 행이 없어 MERGE를 건너뜀({round(time.time() - t0, 1)}초)")
            continue
        spark.script(incoming_sql(t, view))
        spark.script((HERE / t / "2_merge.sql").read_text(encoding="utf-8"))
        log(f"{t}: {mode} 구간 {start} → {end}, Bronze 행 {n}, MERGE 끝({round(time.time() - t0, 1)}초)")
        if a.fail_after_merge:
            log("시험용으로 progress를 저장하기 전에 멈춥니다")
            sys.exit(3)
        prog[t] = (end, mode, n)
        save_progress(spark, prog)

    if a.compare:
        bad = 0
        for t in tables:
            if ends.get(t) is None:
                continue
            x, y = compare(spark, t, ends[t])
            log(f"전체 재계산과 비교 {t}: Silver에만 {x} / 전체에만 {y}")
            bad += x + y
        if bad:
            sys.exit(1)


if __name__ == "__main__":
    main()
