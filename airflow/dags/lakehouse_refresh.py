"""화면 매매 옮기기 → Bronze 기다리기 → Silver MERGE → dbt build → 증분 Gold → 화면 데이터 내보내기 → 화면 매매 대조를
30분마다 한 번(8회차에 만들고, 11/18에 화면 쪽 셋을 붙임).

Bronze는 Flink가 계속 쌓으므로 이 DAG에 없습니다. 태스크는 모두 같은 입력으로 다시 돌려도 답이 같습니다.
  trades_to_source  화면에 저장한 매매를 원천 주문, 체결, 잔고(계좌 A-SCREEN)로 옮김(loaders/screen_trades.py).
                    값이 같은 줄은 고치지 않아 CDC 이벤트가 생기지 않음. 마지막 줄의 바꾼 키 목록이 XCom으로 감
  wait_bronze       그 키의 CDC 이벤트가 모두 Bronze에 닿을 때까지 15초마다 봄(BashSensor, 종료코드 99 = 아직).
                    바꾼 것이 없으면 바로 넘어감. Debezium, Kafka, Flink 중 하나가 멈춰 있으면 10분 뒤 실패
  silver_merge      Silver 현재 상태 다섯 테이블 MERGE(sql/silver/run.py). 지난번 뒤에 더해진 Bronze 행만 읽음
                    (증분 읽기, 11/18 규모 시험 뒤). 새 행이 없으면 MERGE를 건너뜀. 검사에 걸리면 실패
  dbt_build         dbt 모델과 데이터 테스트(정답지인 전체 재계산 Gold, 손익 마트 dbt/models/pnl 포함)
  gold_incremental  일별 종목별 체결 대금 증분(sql/gold_incremental/run.py)과 정답지 대조. 0 / 0이 아니면 실패
  screen_export     Gold 마트에서 화면 데이터 파일을 내보내고 원천, 원본과 대조(ai/screen_data.py). 내용이 같고 검사가
                    통과하면 지금 파일을 그대로 둠(켜 둔 화면 서버가 읽는 파일이 바뀌지 않음)
  trades_verify     Gold 체결과 손익 마트(pnl_position)가 화면 매매, 화면 손익과 같은지. 다르면 실패.
                    결과는 data/generated/f_screen/trades_lake.json(화면 데이터 탭 「내 매매가 지나온 길」)

Airflow는 실패한 태스크를 다시 불러 줄 뿐이고, 다시 불렸을 때 답이 같은지는 각 작업이 지킵니다
(MERGE의 버전 가드, plan 먼저 progress 나중, 적재기의 값 비교, docs/guide/06-incremental-and-backfill.md 13절).
시험용 매매 파일은 수동 실행의 설정 {"trades": "data/generated/f_screen/<파일>.json"}으로 넣습니다(data/generated/ 아래만).
데이터 기간을 늘린 뒤에는 DAG window_extend가 이 DAG를 window_extend__<그 실행> 이름으로 부릅니다(docs/decisions/0016).
"""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.models.param import Param
from airflow.operators.bash import BashOperator
from airflow.sensors.bash import BashSensor

VENV = "/opt/dbt-venv/bin"
APP = "/opt/app-venv/bin/python"   # 화면 매매 적재기와 화면 데이터 내보내기(duckdb, psycopg, pyhive)
WORK = "/workspace"
# 수동 실행에서 고른 매매 파일. 셸 명령에 바로 넣지 않고 환경 변수로 넘깁니다
TRADES_ENV = {"TRADES": "{{ params.trades }}"}
TRADES_ARG = '${TRADES:+--trades "$TRADES"}'

# 테이블마다 progress의 Bronze 스냅샷 뒤에 더해진 행만 읽어 1_incoming.sql, 2_merge.sql을 돌림(docs/decisions/0011)
silver_cmd = f"{VENV}/python {WORK}/sql/silver/run.py"

with DAG(
    dag_id="lakehouse_refresh",
    description="화면 매매 옮기기, Silver MERGE, dbt build, 증분 Gold, 화면 데이터 내보내기와 대조",
    schedule="*/30 * * * *",
    start_date=datetime(2026, 11, 1),
    catchup=False,               # 증분은 Airflow의 날짜 구간이 아니라 progress부터 읽으므로 밀린 실행을 메우지 않음
    max_active_runs=1,           # 두 실행이 같은 progress를 동시에 읽고 덮어쓰지 않게
    is_paused_upon_creation=True,
    dagrun_timeout=timedelta(minutes=28),
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=1),   # 실습에서 기다리기 좋게 짧게. 운영이라면 3분 이상
    },
    params={"trades": Param("", type="string",
                            description="시험용 매매 파일(data/generated/ 아래). 비우면 화면 저장 파일")},
    tags=["cdc", "iceberg", "dbt", "screen"],
) as dag:
    trades_to_source = BashOperator(
        task_id="trades_to_source",
        bash_command=f"cd {WORK} && {APP} loaders/screen_trades.py {TRADES_ARG}",
        env=TRADES_ENV, append_env=True,
    )

    # 앞 태스크가 마지막 줄에 찍은 키 목록(XCom). BashSensor는 환경 변수를 통째로 바꾸므로 필요한 것을 모두 넣습니다
    wait_bronze = BashSensor(
        task_id="wait_bronze",
        bash_command=f'cd {WORK} && {APP} loaders/screen_trades.py --wait-bronze "$CHANGES"',
        env={"CHANGES": "{{ ti.xcom_pull(task_ids='trades_to_source') }}", "SPARK_THRIFT_HOST": "spark",
             "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/local/bin:/usr/bin:/bin"},
        retry_exit_code=99,      # 99 = 아직(다시 봄). 그 밖의 0 아닌 값은 오류로 바로 실패
        poke_interval=15,
        timeout=600,
        mode="poke",
        retries=0,               # 제한 시간에 걸린 것은 다시 불러도 같으므로 바로 실패로 둠
    )

    silver_merge = BashOperator(task_id="silver_merge", bash_command=silver_cmd)

    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"cd {WORK}/dbt && {VENV}/dbt build",
    )

    gold_incremental = BashOperator(
        task_id="gold_incremental",
        bash_command=f"{VENV}/python {WORK}/sql/gold_incremental/run.py --compare",
    )

    screen_export = BashOperator(
        task_id="screen_export",
        bash_command=f"cd {WORK} && {APP} -m ai.screen_data export --if-changed",
    )

    trades_verify = BashOperator(
        task_id="trades_verify",
        bash_command=f"cd {WORK} && {APP} loaders/screen_trades.py --verify {TRADES_ARG}",
        env=TRADES_ENV, append_env=True,
    )

    (trades_to_source >> wait_bronze >> silver_merge >> dbt_build >> gold_incremental
     >> screen_export >> trades_verify)
