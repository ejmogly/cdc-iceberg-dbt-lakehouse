#!/usr/bin/env python3
"""일별 종목별 체결 대금의 증분 갱신(바뀐 키만 다시 계산).

docs/guide/06-incremental-and-backfill.md 7 ~ 12절, 14절의 설계를 이 저장소에 옮겼습니다.

  1. 구간 고정: 입력(Silver executions, orders)마다 progress에 저장한 스냅샷이 시작(빼고),
     main 참조의 현재 스냅샷이 끝(넣고)입니다. 계산은 모두 끝 스냅샷으로 고정해 읽습니다.
  2. plan 저장: 구간의 변경(create_changelog_view)에서 영향 키 (symbol, trade_date)를 모아
     range_id와 함께 저장합니다. 같은 range_id가 이미 있으면 다시 계산하지 않고 그대로 씁니다.
  3. Gold MERGE: 영향 키를 왼쪽에 두고 끝 스냅샷으로 처음부터 다시 합산합니다.
     체결이 0건이 된 키는 지웁니다.
  4. progress 저장: 두 입력의 끝 스냅샷을 마지막에 저장합니다.

progress가 없거나(첫 실행), 구간의 스냅샷이 만료돼 변경을 읽을 수 없거나, --full이면
모든 키를 다시 계산합니다(비상 경로). 정답지는 dbt table 모델 gold.mart_daily_symbol_trading이고
--compare가 두 테이블을 모든 칸으로 비교해 "증분에만 / 정답지에만" 행 수를 찍습니다.

dbt 컨테이너(pyhive가 있음)에서 돌립니다.
  docker compose --profile spark run --rm --entrypoint python dbt \\
    /workspace/sql/gold_incremental/run.py --compare
"""
import argparse
import hashlib
import os
import sys

from pyhive import hive

TARGET = "lake.gold.mart_daily_symbol_trading_inc"
PLAN = "lake.gold.mart_daily_symbol_trading_inc_plan"
PROGRESS = "lake.gold.mart_daily_symbol_trading_inc_progress"
ORACLE = "lake.gold.mart_daily_symbol_trading"
# 입력 테이블과 변경 뷰를 만들 때 쓰는 키
INPUTS = {"silver.executions": "execution_id", "silver.orders": "order_id"}
COLUMNS = "symbol, market, currency, trade_date, execution_count, traded_qty, traded_value"


class Spark:
    def __init__(self):
        self.conn = hive.connect(
            host=os.environ.get("SPARK_THRIFT_HOST", "spark"),
            port=int(os.environ.get("SPARK_THRIFT_PORT", "10000")),
            username="incremental", auth="NOSASL")
        self.cur = self.conn.cursor()

    def run(self, sql):
        """결과를 읽지 않는 문장(CREATE, INSERT, MERGE, CALL)"""
        self.cur.execute(sql)

    def rows(self, sql):
        self.cur.execute(sql)
        return self.cur.fetchall()

    def one(self, sql):
        rows = self.rows(sql)
        return rows[0][0] if rows else None


def log(msg):
    print(msg, flush=True)


def create_tables(spark):
    spark.run(f"""
        CREATE TABLE IF NOT EXISTS {TARGET} (
          symbol STRING, market STRING, currency STRING, trade_date DATE,
          execution_count BIGINT, traded_qty BIGINT, traded_value DECIMAL(38,4)
        ) USING iceberg TBLPROPERTIES ('format-version' = '2')""")
    spark.run(f"""
        CREATE TABLE IF NOT EXISTS {PLAN} (
          range_id STRING, symbol STRING, trade_date DATE, planned_at TIMESTAMP
        ) USING iceberg TBLPROPERTIES ('format-version' = '2')""")
    spark.run(f"""
        CREATE TABLE IF NOT EXISTS {PROGRESS} (
          input_table STRING, snapshot_id BIGINT, range_id STRING, done_at TIMESTAMP
        ) USING iceberg TBLPROPERTIES ('format-version' = '2')""")


def fix_range(spark):
    """1. 구간 고정: 입력마다 (시작, 끝) 스냅샷과 range_id"""
    starts = dict(spark.rows(f"SELECT input_table, snapshot_id FROM {PROGRESS}"))
    ends = {t: spark.one(f"SELECT snapshot_id FROM lake.{t}.refs WHERE name = 'main'") for t in INPUTS}
    text = "|".join(f"{t}:{starts.get(t)}:{ends[t]}" for t in sorted(INPUTS))
    range_id = hashlib.sha256(text.encode()).hexdigest()[:16]
    return starts, ends, range_id


def define_core(spark, ends):
    """끝 스냅샷으로 고정한 체결 사실. dbt의 dim_order, fct_execution과 같은 조건과 날짜 규칙입니다.
    규칙을 바꾸면 두 곳을 함께 고치고, --compare가 0 / 0인지 봅니다."""
    spark.run(f"""
        CREATE OR REPLACE TEMPORARY VIEW inc_core_execution AS
        SELECT o.symbol, i.market, i.currency,
               to_date(from_utc_timestamp(e.executed_at, m.tz)) AS trade_date,
               e.exec_qty, e.exec_qty * e.exec_price AS exec_value
        FROM (SELECT * FROM lake.silver.executions VERSION AS OF {ends['silver.executions']}
              WHERE NOT is_deleted) e
        LEFT JOIN (SELECT * FROM lake.silver.orders VERSION AS OF {ends['silver.orders']}
                   WHERE NOT is_deleted) o
          ON e.order_id = o.order_id
        LEFT JOIN lake.silver.instruments i ON o.symbol = i.symbol
        LEFT JOIN lake.silver.markets m ON i.market = m.market""")
    spark.run("""
        CREATE OR REPLACE TEMPORARY VIEW inc_full AS
        SELECT symbol, market, currency, trade_date,
               count(*) AS execution_count, sum(exec_qty) AS traded_qty,
               CAST(sum(exec_value) AS DECIMAL(38,4)) AS traded_value
        FROM inc_core_execution
        GROUP BY symbol, market, currency, trade_date""")
    # 거래일을 정하는 시간대(종목 → 시장 → 시간대)
    spark.run("""
        CREATE OR REPLACE TEMPORARY VIEW inc_symbol_tz AS
        SELECT i.symbol, m.tz
        FROM lake.silver.instruments i JOIN lake.silver.markets m ON i.market = m.market""")


def changed_inputs(spark, starts, ends):
    """구간의 변경 뷰를 만듭니다. 바뀐 입력의 이름 목록을 돌려줍니다.
    시작 스냅샷이 만료돼 사라졌으면 예외를 내고, 부르는 쪽이 전체 재계산으로 돌립니다(가이드 07 8절).
    뷰를 만드는 CALL은 성공해도 읽을 때 지워진 파일에서 실패할 수 있어, 먼저 스냅샷이 남았는지 봅니다."""
    changed = []
    for table, key in INPUTS.items():
        start, end = starts.get(table), ends[table]
        if start == end:
            continue
        if not spark.one(f"SELECT count(*) FROM lake.{table}.snapshots WHERE snapshot_id = {start}"):
            raise RuntimeError(f"{table}의 progress 스냅샷 {start}가 만료돼 없습니다")
        view = "inc_changes_" + table.split(".")[1]
        options = f"'start-snapshot-id', '{start}', 'end-snapshot-id', '{end}'"
        spark.run(f"""
            CALL lake.system.create_changelog_view(
              table => '{table}',
              options => map({options}),
              changelog_view => '{view}',
              identifier_columns => array('{key}'),
              compute_updates => true)""")
        changed.append(table)
    return changed


def affected_sql(changed, ends):
    """영향 키 (symbol, trade_date). 변경 뷰의 모든 행(바뀌기 전 행과 뒤 행, 삭제 표시 포함)을 씁니다.
    체결 시각이 고쳐지면 바뀌기 전 행이 옛 거래일을, 뒤 행이 새 거래일을 냅니다(가이드 06 9절, 14절)."""
    parts = []
    if "silver.executions" in changed:
        # 체결 → 주문(끝 스냅샷) → 종목. 철회된 체결도 order_id와 체결 시각이 남아 있습니다(0005).
        parts.append(f"""
            SELECT o.symbol, to_date(from_utc_timestamp(c.executed_at, z.tz)) AS trade_date
            FROM inc_changes_executions c
            JOIN lake.silver.orders VERSION AS OF {ends['silver.orders']} o ON c.order_id = o.order_id
            JOIN inc_symbol_tz z ON o.symbol = z.symbol""")
    if "silver.orders" in changed:
        # 주문의 종목이 바뀌면 바뀌기 전 행의 종목도 영향을 받습니다. 그 주문의 체결 거래일마다.
        parts.append(f"""
            SELECT c.symbol, to_date(from_utc_timestamp(e.executed_at, z.tz)) AS trade_date
            FROM inc_changes_orders c
            JOIN lake.silver.executions VERSION AS OF {ends['silver.executions']} e ON c.order_id = e.order_id
            JOIN inc_symbol_tz z ON c.symbol = z.symbol""")
    return "\nUNION\n".join(parts)


def save_plan(spark, range_id, starts, ends, full):
    """2. plan 저장. 같은 range_id의 plan이 있으면 그대로 씁니다."""
    if spark.one(f"SELECT count(*) FROM {PLAN} WHERE range_id = '{range_id}'"):
        log(f"plan: range_id {range_id}의 저장된 plan을 다시 씁니다")
        return
    mode = "full" if full else "changes"
    changed = []
    if not full:
        try:
            changed = changed_inputs(spark, starts, ends)
        except Exception as e:  # 만료된 스냅샷 등
            log(f"변경을 읽지 못해 전체 재계산으로 돌립니다: {str(e).splitlines()[0][:200]}")
            mode = "full"
    if mode == "changes" and not changed:
        log("plan: 구간에 바뀐 행이 없습니다")
        return
    full_source = f"SELECT symbol, trade_date FROM inc_full UNION SELECT symbol, trade_date FROM {TARGET}"

    def insert(source):
        # INSERT 한 번이 commit 한 번이라, 도중에 실패하면 plan에는 아무것도 남지 않습니다.
        spark.run(f"""
            INSERT INTO {PLAN}
            SELECT DISTINCT '{range_id}', symbol, trade_date, current_timestamp()
            FROM ({source}) k
            WHERE symbol IS NOT NULL AND trade_date IS NOT NULL""")

    if mode == "changes":
        try:
            insert(affected_sql(changed, ends))
        except Exception as e:  # 변경 뷰를 읽다 지워진 파일을 만난 경우 등
            log(f"변경을 읽지 못해 전체 재계산으로 돌립니다: {str(e).splitlines()[0][:200]}")
            mode = "full"
    if mode == "full":
        insert(full_source)
    log(f"plan: {mode} 모드로 저장")


def merge_gold(spark, range_id):
    """3. 영향 키를 왼쪽에 둔 MERGE. 끝 스냅샷에 체결이 없는 키는 present가 거짓이라 지웁니다."""
    spark.run(f"""
        MERGE INTO {TARGET} t
        USING (
          SELECT k.symbol, k.trade_date, n.market, n.currency,
                 n.execution_count, n.traded_qty, n.traded_value,
                 n.symbol IS NOT NULL AS present
          FROM (SELECT DISTINCT symbol, trade_date FROM {PLAN} WHERE range_id = '{range_id}') k
          LEFT JOIN inc_full n
            ON k.symbol = n.symbol AND k.trade_date = n.trade_date
        ) s
        ON t.symbol = s.symbol AND t.trade_date = s.trade_date
        WHEN MATCHED AND NOT s.present THEN DELETE
        WHEN MATCHED THEN UPDATE SET
          t.market = s.market, t.currency = s.currency,
          t.execution_count = s.execution_count,
          t.traded_qty = s.traded_qty, t.traded_value = s.traded_value
        WHEN NOT MATCHED AND s.present THEN INSERT ({COLUMNS})
          VALUES (s.symbol, s.market, s.currency, s.trade_date,
                  s.execution_count, s.traded_qty, s.traded_value)""")


def save_progress(spark, ends, range_id):
    """4. progress 저장. INSERT OVERWRITE 한 번이라 두 입력의 값이 한 commit으로 바뀝니다."""
    values = ", ".join(f"('{t}', {ends[t]}, '{range_id}', current_timestamp())" for t in sorted(INPUTS))
    spark.run(f"INSERT OVERWRITE TABLE {PROGRESS} VALUES {values}")


def compare(spark):
    """정답지(dbt 전체 재계산)와 모든 칸 비교. (증분에만, 정답지에만) 행 수"""
    cols = "symbol, market, currency, trade_date, execution_count, traded_qty, CAST(traded_value AS DECIMAL(38,4))"
    only_inc = spark.one(f"SELECT count(*) FROM (SELECT {cols} FROM {TARGET} EXCEPT SELECT {cols} FROM {ORACLE})")
    only_oracle = spark.one(f"SELECT count(*) FROM (SELECT {cols} FROM {ORACLE} EXCEPT SELECT {cols} FROM {TARGET})")
    return only_inc, only_oracle


def main():
    p = argparse.ArgumentParser(description="일별 종목별 체결 대금의 증분 갱신")
    p.add_argument("--full", action="store_true", help="모든 키를 다시 계산(비상 경로)")
    p.add_argument("--compare", action="store_true", help="끝난 뒤 정답지와 비교, 다르면 1로 끝남")
    p.add_argument("--fail-after-merge", action="store_true",
                   help="시험용: Gold를 고친 뒤 progress 저장 전에 멈춤(가이드 06 12절 s6)")
    args = p.parse_args()

    spark = Spark()
    create_tables(spark)
    starts, ends, range_id = fix_range(spark)
    for t in sorted(INPUTS):
        log(f"구간 {t}: {starts.get(t)} → {ends[t]}")
    log(f"range_id {range_id}")

    if starts and all(starts.get(t) == ends[t] for t in INPUTS) and not args.full:
        log("새 스냅샷이 없어 할 일이 없습니다")
    else:
        define_core(spark, ends)
        save_plan(spark, range_id, starts, ends, full=args.full or not starts)
        keys = spark.rows(f"SELECT symbol, CAST(trade_date AS STRING) FROM {PLAN} "
                         f"WHERE range_id = '{range_id}' ORDER BY 1, 2") or []
        log(f"영향 키 {len(keys)}개: " + ", ".join(f"({s}, {d})" for s, d in keys[:20]))
        if keys:
            merge_gold(spark, range_id)
            log("Gold MERGE 끝")
        if args.fail_after_merge:
            log("시험용으로 progress를 저장하기 전에 멈춥니다")
            sys.exit(3)
        save_progress(spark, ends, range_id)
        log("progress 저장 끝")

    if args.compare:
        only_inc, only_oracle = compare(spark)
        log(f"정답지와 비교: 증분에만 {only_inc} / 정답지에만 {only_oracle}")
        if only_inc or only_oracle:
            sys.exit(1)


if __name__ == "__main__":
    main()
