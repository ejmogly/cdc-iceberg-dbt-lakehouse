# dbt(dbt-spark, Thrift 접속). Spark Thrift server에 붙어 Gold를 만듭니다.
#   docker compose --profile spark run --rm dbt build
FROM python:3.11-slim

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
      -r /tmp/requirements.lock

# 사용 통계를 밖으로 보내지 않습니다.
ENV DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    DO_NOT_TRACK=1

ENTRYPOINT ["dbt"]
CMD ["--version"]
