# Airflow standalone + dbt 전용 가상환경(8회차) + 화면 매매와 화면 데이터용 가상환경(11/18)
# Docker Hub의 apache/airflow 이미지에, dbt 컨테이너와 같은 lock 파일(infra/dbt/requirements.lock)로
# dbt-spark와 pyhive를 따로 된 가상환경에 넣습니다. Airflow의 의존성과 섞이지 않게 하려는 것입니다.
# build context는 infra/입니다(docker-compose.yml).
FROM apache/airflow:2.10.5-python3.11

USER root
COPY dbt/requirements.lock /tmp/dbt-requirements.lock
# 이미지가 pip을 사용자 영역 설치로 맞춰 두어, 가상환경 안에서는 그 설정을 끕니다.
RUN PIP_USER=false python -m venv /opt/dbt-venv \
 && PIP_USER=false /opt/dbt-venv/bin/pip install --no-cache-dir --disable-pip-version-check \
      -r /tmp/dbt-requirements.lock \
 && rm /tmp/dbt-requirements.lock
# 화면 매매 적재기와 화면 데이터 내보내기용 가상환경(duckdb, psycopg, pyhive). dbt 가상환경과 섞지 않습니다(11/18).
COPY airflow/app-requirements.lock /tmp/app-requirements.lock
RUN PIP_USER=false python -m venv /opt/app-venv \
 && PIP_USER=false /opt/app-venv/bin/pip install --no-cache-dir --disable-pip-version-check \
      -r /tmp/app-requirements.lock \
 && rm /tmp/app-requirements.lock
USER airflow

ENV DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    DO_NOT_TRACK=1
