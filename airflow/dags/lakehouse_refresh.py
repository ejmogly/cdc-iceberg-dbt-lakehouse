"""Silver MERGE → dbt build → 증분 Gold를 30분마다 한 번(8회차).

Bronze는 Flink가 계속 쌓으므로 이 DAG에 없습니다. 태스크 셋은 모두 같은 입력으로 다시 돌려도 답이 같습니다.
  silver_merge      Silver 현재 상태 다섯 테이블 MERGE(sql/silver). 검사에 걸리면 실패
  dbt_build         dbt seed, 모델, 데이터 테스트(정답지인 전체 재계산 Gold 포함)
  gold_incremental  일별 종목별 체결 대금 증분(sql/gold_incremental/run.py)과 정답지 대조. 0 / 0이 아니면 실패

Airflow는 실패한 태스크를 다시 불러 줄 뿐이고, 다시 불렸을 때 답이 같은지는 각 작업이 지킵니다
(MERGE의 버전 가드, plan 먼저 progress 나중, docs/guide/06-incremental-and-backfill.md 13절).
"""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

SILVER_TABLES = ["orders", "executions", "holdings", "daily_prices", "fx_rates"]
VENV = "/opt/dbt-venv/bin"
WORK = "/workspace"

# 테이블마다 1_incoming.sql과 2_merge.sql을 한 세션에서
silver_cmd = "set -e\n" + "\n".join(
    f"{VENV}/python {WORK}/infra/spark/run_sql.py "
    f"{WORK}/sql/silver/{t}/1_incoming.sql {WORK}/sql/silver/{t}/2_merge.sql"
    for t in SILVER_TABLES)

with DAG(
    dag_id="lakehouse_refresh",
    description="Silver MERGE, dbt build, 증분 Gold",
    schedule="*/30 * * * *",
    start_date=datetime(2026, 11, 1),
    catchup=False,               # 증분은 Airflow의 날짜 구간이 아니라 progress부터 읽으므로 밀린 실행을 메우지 않음
    max_active_runs=1,           # 두 실행이 같은 progress를 동시에 읽고 덮어쓰지 않게
    is_paused_upon_creation=True,
    dagrun_timeout=timedelta(minutes=25),
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=1),   # 실습에서 기다리기 좋게 짧게. 운영이라면 3분 이상
    },
    tags=["cdc", "iceberg", "dbt"],
) as dag:
    silver_merge = BashOperator(task_id="silver_merge", bash_command=silver_cmd)

    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"cd {WORK}/dbt && {VENV}/dbt build",
    )

    gold_incremental = BashOperator(
        task_id="gold_incremental",
        bash_command=f"{VENV}/python {WORK}/sql/gold_incremental/run.py --compare",
    )

    silver_merge >> dbt_build >> gold_incremental
