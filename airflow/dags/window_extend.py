"""화면 데이터 기간 늘리기: Mac이 받아 둔 새 구간 원본을 확인하고 원천에 넣은 뒤 구간 목록에 더하고, lakehouse_refresh로
Silver, Gold, 화면 파일까지 새로 만듭니다(11/19, docs/decisions/0016).

받기는 이 DAG에 없습니다. HTTPS를 검사하는 프록시가 있는 네트워크에서는 컨테이너가 시세 API의 인증서를 믿지 못할 수 있고,
키는 Mac의 .env에만 둡니다.
Mac의 loaders/extend_window.py fetch(매일 14:30, 또는 make window-fetch)가 data/raw/window/에 원본 38개를 받고 다 받은 뒤에
next.json(구간과 파일 지문)을 적으면, 이 DAG가 그것을 기다렸다가 처리합니다. 컨테이너는 원본을 읽기만 합니다.
  wait_raw       next.json에 구간 끝 뒤의 새 원본이 적힐 때까지 5분마다 봄(BashSensor, reschedule라 기다리는 동안 자리를 비움).
                 3시간 안에 오지 않으면 건너뜀(soft_fail, 주말과 휴장일에는 새 원본이 없음)
  check_raw      파일 지문이 next.json과 같은지(받는 중이거나 바뀐 파일), 국내 끝날, 앞 구간과 겹치는 날의 값이 같은지.
                 마지막 줄 「시작 끝」이 XCom으로 감
  load_source    같은 원본으로 원천 시세(국내, 미국)와 환율을 넣음(--from-raw). 값이 같은 행은 고치지 않아 CDC 이벤트가 없음.
                 마지막 줄의 바뀐 행 수와 lsn 기준점이 XCom으로 감
  wait_bronze    바뀐 행이 모두 Bronze에 닿을 때까지 15초마다 봄. 닿기 전에 구간을 더하면 새로 고침이 반쯤 온 Gold를 원본과
                 견주어 실패하므로 먼저 기다림
  add_part       data/generated/window.json에 구간을 더함(이미 있으면 그대로). 이 뒤의 화면 내보내기부터 기간이 늚
  refresh_lake   lakehouse_refresh를 부름. 실행 이름을 window_extend__<이 실행>으로 고정해, 이 태스크가 다시 돌아도 실행이
                 하나만 생김(이미 있으면 그대로). lakehouse_refresh가 꺼져 있으면(paused) 건너뜀. 그때는 make lake-refresh-test
  wait_refresh   부른 실행이 끝날 때까지 30초마다 봄(PythonSensor, reschedule라 SequentialExecutor의 한 자리를 비워 줌).
                 실패하면 바로 실패
  confirm        화면 파일의 기간 끝이 새 끝날이고 검사가 모두 통과했는지

태스크는 모두 같은 입력으로 다시 돌려도 답이 같습니다(지문 대조, 값 비교 적재, 이미 있으면 그대로 두는 구간 더하기).
"""
from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.exceptions import AirflowFailException, AirflowSkipException, DagRunAlreadyExists
from airflow.operators.bash import BashOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sensors.bash import BashSensor
from airflow.sensors.python import PythonSensor

APP = "/opt/app-venv/bin/python"
WORK = "/workspace"
EW = f"cd {WORK} && {APP} loaders/extend_window.py"
PART = {"PART": "{{ ti.xcom_pull(task_ids='check_raw') }}"}   # 「시작 끝」. 셸 명령에 바로 넣지 않고 환경 변수로
REFRESH = "lakehouse_refresh"
REFRESH_RUN = "window_extend__{{ run_id }}"


class RefreshIfOn(TriggerDagRunOperator):
    """lakehouse_refresh가 켜져 있을 때만 부릅니다(꺼진 DAG의 실행은 scheduler가 시작하지 않아 끝없이 기다림).
    같은 이름의 실행이 이미 있으면 새로 만들지 않고 넘어갑니다. 11/19 시험에서 기다림(deferrable)을 airflow dags test로
    돌리자 이 태스크가 되풀이되어 실행 이름(지금 시각)마다 2,017개가 쌓였습니다. 이름을 고정하면 되풀이돼도 하나입니다."""

    def execute(self, context):
        from airflow.models import DagModel
        dag = DagModel.get_dagmodel(self.trigger_dag_id)
        if dag is None or dag.is_paused:
            raise AirflowSkipException(f"{self.trigger_dag_id}가 꺼져 있어(paused) 새로 고침을 건너뜁니다. "
                                       "make lake-refresh-test로 따로 돌리세요")
        try:
            return super().execute(context)
        except DagRunAlreadyExists:
            self.log.info("%s 실행 %s가 이미 있어 그대로 둡니다", self.trigger_dag_id, self.trigger_run_id)


def refresh_done(run_id):
    """부른 lakehouse_refresh 실행이 끝났으면 True, 실패했으면 바로 실패, 아직이면 False(센서가 다시 봄)."""
    from airflow.models import DagRun
    runs = DagRun.find(dag_id=REFRESH, run_id=run_id)
    if not runs:
        raise AirflowFailException(f"{REFRESH} 실행 {run_id}가 없습니다")
    state = runs[0].state
    if state == "failed":
        raise AirflowFailException(f"{REFRESH} 실행 {run_id}가 실패했습니다. 그 실행의 태스크 로그를 보세요")
    print(f"{REFRESH} {run_id}: {state}")
    return state == "success"


with DAG(
    dag_id="window_extend",
    description="Mac이 받은 새 구간 원본을 확인, 원천 적재, Bronze 기다림, 구간 더하기, 레이크 새로 고침, 화면 확인",
    schedule="40 14 * * *",          # Mac의 받기(14:30)보다 조금 뒤. 국내 시세는 다음 영업일 13시 뒤에 나옴
    start_date=pendulum.datetime(2026, 11, 19, tz="Asia/Seoul"),
    catchup=False,                   # 밀린 날을 하루씩 메우지 않음. 받기가 마지막 구간 끝부터 한 번에 받으므로
    max_active_runs=1,
    is_paused_upon_creation=True,
    dagrun_timeout=timedelta(hours=4),
    default_args={"retries": 1, "retry_delay": timedelta(minutes=1)},
    tags=["cdc", "screen", "window"],
) as dag:
    wait_raw = BashSensor(
        task_id="wait_raw",
        bash_command=f"{EW} pending",
        retry_exit_code=99,          # 99 = 아직(다시 봄). 2 = 앞뒤가 안 맞음(바로 실패)
        poke_interval=300,
        timeout=3 * 60 * 60,
        mode="reschedule",
        soft_fail=True,
        retries=0,
    )

    check_raw = BashOperator(task_id="check_raw", bash_command=f"{EW} check")

    load_source = BashOperator(
        task_id="load_source",
        bash_command=f"{EW} load $PART",
        env=PART, append_env=True,
    )

    # BashSensor는 환경 변수를 통째로 바꾸므로 필요한 것을 모두 넣습니다(lakehouse_refresh의 wait_bronze와 같음)
    wait_bronze = BashSensor(
        task_id="wait_bronze",
        bash_command=f'{EW} wait-bronze "$CHANGES"',
        env={"CHANGES": "{{ ti.xcom_pull(task_ids='load_source') }}", "SPARK_THRIFT_HOST": "spark",
             "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/local/bin:/usr/bin:/bin"},
        retry_exit_code=99,
        poke_interval=15,
        timeout=600,
        mode="poke",
        retries=0,
    )

    add_part = BashOperator(
        task_id="add_part",
        bash_command=f"{EW} add $PART",
        env=PART, append_env=True,
    )

    refresh_lake = RefreshIfOn(
        task_id="refresh_lake",
        trigger_dag_id=REFRESH,
        trigger_run_id=REFRESH_RUN,
        wait_for_completion=False,   # 기다림은 아래 센서가 함. 기다림(deferrable)은 airflow dags test에서 되풀이됨
    )

    wait_refresh = PythonSensor(
        task_id="wait_refresh",
        python_callable=refresh_done,
        op_kwargs={"run_id": REFRESH_RUN},
        poke_interval=30,
        timeout=40 * 60,
        mode="reschedule",
        retries=0,
    )

    confirm = BashOperator(
        task_id="confirm",
        bash_command=f'{EW} confirm "${{PART#* }}"',
        env=PART, append_env=True,
    )

    wait_raw >> check_raw >> load_source >> wait_bronze >> add_part >> refresh_lake >> wait_refresh >> confirm
