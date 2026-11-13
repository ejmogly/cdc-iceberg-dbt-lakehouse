#!/usr/bin/env python3
"""Iceberg 테이블 하나의 유지보수 한 주기(docs/guide/07-iceberg-maintenance.md 7 ~ 10절).

순서: rewrite_data_files → expire_snapshots → remove_orphan_files → rewrite_manifests
단계마다 파일, 스냅샷, manifest, metadata 이력 수와 행 수, 합계를 재고, 행 수나 합계가 바뀌면 멈춥니다.

스냅샷 만료 기준(older_than)은 아래 둘 가운데 이른 쪽입니다(가이드 07 9절).
  - 이 테이블을 읽는 증분 작업의 progress 스냅샷 커밋 시각(그 스냅샷과 뒤는 남김)
  - 지금 - 보존 기간(--retain-minutes, 기본 3일)
고아 정리는 24시간보다 최근 파일을 보지 않습니다(Iceberg가 거부). 기본은 3일 전입니다.
Flink가 쓰는 Bronze 테이블은 Flink 작업을 멈춘 뒤에만 돌립니다(가이드 07 10절).

dbt 컨테이너(pyhive가 있음)에서 돌립니다.
  docker compose --profile spark run --rm --entrypoint python dbt \\
    /workspace/sql/maintenance/maintain.py silver.executions \\
    --sum "exec_qty * exec_price"
"""
import argparse
import os
import sys

from pyhive import hive

# 이 테이블을 증분으로 읽는 작업의 progress 테이블(sql/gold_incremental/run.py)
PROGRESS_TABLES = ["lake.gold.mart_daily_symbol_trading_inc_progress"]


class Spark:
    def __init__(self):
        self.conn = hive.connect(
            host=os.environ.get("SPARK_THRIFT_HOST", "spark"),
            port=int(os.environ.get("SPARK_THRIFT_PORT", "10000")),
            username="maintenance", auth="NOSASL")
        self.cur = self.conn.cursor()

    def rows(self, sql):
        self.cur.execute(sql)
        return self.cur.fetchall() or []

    def one(self, sql):
        rows = self.rows(sql)
        return rows[0][0] if rows else None


def measure(spark, t, sum_expr):
    q = f"lake.{t}"
    return {
        "rows": spark.one(f"SELECT count(*) FROM {q}"),
        "sum": str(spark.one(f"SELECT sum({sum_expr}) FROM {q}")),
        "data_files": spark.one(f"SELECT count(*) FROM {q}.files"),
        "all_data_files": spark.one(f"SELECT count(DISTINCT file_path) FROM {q}.all_data_files"),
        "snapshots": spark.one(f"SELECT count(*) FROM {q}.snapshots"),
        "manifests": spark.one(f"SELECT count(*) FROM {q}.manifests"),
        "all_manifests": spark.one(f"SELECT count(DISTINCT path) FROM {q}.all_manifests"),
        "metadata_log": spark.one(f"SELECT count(*) FROM {q}.metadata_log_entries"),
    }


def progress_snapshots(spark, t):
    """이 테이블을 가리키는 progress 스냅샷과 그 커밋 시각"""
    found = []
    for p in PROGRESS_TABLES:
        try:
            ids = [r[0] for r in spark.rows(f"SELECT snapshot_id FROM {p} WHERE input_table = '{t}'")]
        except Exception:
            continue  # progress 테이블이 아직 없음
        for sid in ids:
            ts = spark.one(f"SELECT CAST(committed_at AS STRING) FROM lake.{t}.snapshots WHERE snapshot_id = {sid}")
            found.append((sid, ts))
    return found


def main():
    p = argparse.ArgumentParser(description="Iceberg 테이블 하나의 유지보수 한 주기")
    p.add_argument("table", help="예: silver.executions")
    p.add_argument("--sum", required=True, help="정리 전후로 대조할 합계 식, 예: exec_qty * exec_price")
    p.add_argument("--retain-minutes", type=int, default=3 * 24 * 60, help="스냅샷 보존 기간(분)")
    p.add_argument("--orphan-hours", type=int, default=72, help="이보다 오래된 고아만 지움(24 이상)")
    args = p.parse_args()
    if args.orphan_hours < 24:
        sys.exit("--orphan-hours는 24 이상이어야 합니다")

    spark = Spark()
    t = args.table
    base = measure(spark, t, args.sum)
    keys = list(base)
    print("단계          " + "  ".join(f"{k:>14}" for k in keys), flush=True)

    def report(step):
        m = measure(spark, t, args.sum)
        print(f"{step:<13} " + "  ".join(f"{str(m[k]):>14}" for k in keys), flush=True)
        if (m["rows"], m["sum"]) != (base["rows"], base["sum"]):
            sys.exit(f"{step}: 행 수나 합계가 바뀌어 멈춥니다 {base['rows']}, {base['sum']} → {m['rows']}, {m['sum']}")

    report("before")

    spark.rows(f"CALL lake.system.rewrite_data_files(table => '{t}', options => map('min-input-files', '2'))")
    report("compact")

    # 만료 기준: progress 커밋 시각과 지금 - 보존 기간 가운데 이른 쪽
    retain_cut = spark.one(f"SELECT CAST(current_timestamp() - INTERVAL {args.retain_minutes} MINUTES AS STRING)")
    cuts = [c for c in [retain_cut] + [ts for _, ts in progress_snapshots(spark, t)] if c]
    older_than = min(cuts)
    print(f"만료 기준 {older_than} (보존 기간 기준 {retain_cut}, progress {progress_snapshots(spark, t)})", flush=True)
    spark.rows(f"CALL lake.system.expire_snapshots(table => '{t}', "
               f"older_than => TIMESTAMP '{older_than}', retain_last => 1)")
    report("expire")

    orphan_cut = spark.one(f"SELECT CAST(current_timestamp() - INTERVAL {args.orphan_hours} HOURS AS STRING)")
    dry = spark.rows(f"CALL lake.system.remove_orphan_files(table => '{t}', "
                     f"older_than => TIMESTAMP '{orphan_cut}', dry_run => true, prefix_listing => true)")
    print(f"고아 후보(기준 {orphan_cut}, dry_run) {len(dry)}개", flush=True)
    if dry:
        spark.rows(f"CALL lake.system.remove_orphan_files(table => '{t}', "
                   f"older_than => TIMESTAMP '{orphan_cut}', prefix_listing => true)")
    report("orphans")

    spark.rows(f"CALL lake.system.rewrite_manifests('{t}')")
    report("manifests")


if __name__ == "__main__":
    main()
