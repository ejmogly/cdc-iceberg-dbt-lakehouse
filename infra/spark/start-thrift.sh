#!/usr/bin/env bash
# Spark Thrift server를 띄웁니다. 기본 카탈로그는 Iceberg JDBC 카탈로그 lake이고,
# Flink(infra/flink/bronze.sql)와 같은 카탈로그 DB와 버킷을 봅니다.
# 접속 정보는 컨테이너 환경 변수(.env에서 옴)로 받습니다.
#
# singleSession=true: 모든 접속(beeline, dbt)이 Spark 세션 하나를 같이 씁니다.
#   접속마다 세션을 새로 만들면 세션마다 Iceberg JDBC 카탈로그가 카탈로그 DB 연결을 새로 열고
#   닫지 않아, 검증 스크립트를 몇 번 돌리면 PostgreSQL 연결 한도(100)를 넘깁니다(2026-11-12 확인).
#   대신 SET과 임시 뷰가 접속 사이에 이어지므로, Silver와 dbt를 동시에 돌리지 않습니다.
# ansi.enabled=true: 글자를 숫자나 시각으로 바꾸다 실패하면 NULL 대신 오류를 냅니다.
#   Silver SQL도 같은 값을 SET하므로 세션을 같이 써도 dbt와 설정이 어긋나지 않습니다.
set -euo pipefail

lake=spark.sql.catalog.lake
exec /opt/spark/bin/spark-submit \
  --class org.apache.spark.sql.hive.thriftserver.HiveThriftServer2 \
  --master 'local[2]' \
  --driver-memory 2g \
  --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
  --conf spark.sql.defaultCatalog=lake \
  --conf "$lake=org.apache.iceberg.spark.SparkCatalog" \
  --conf "$lake.type=jdbc" \
  --conf "$lake.uri=jdbc:postgresql://catalog-db:5432/${CATALOG_DB}" \
  --conf "$lake.jdbc.user=${CATALOG_USER}" \
  --conf "$lake.jdbc.password=${CATALOG_PASSWORD}" \
  --conf "$lake.warehouse=s3://${S3_BUCKET}/" \
  --conf "$lake.io-impl=org.apache.iceberg.aws.s3.S3FileIO" \
  --conf "$lake.s3.endpoint=http://s3:9000" \
  --conf "$lake.s3.path-style-access=true" \
  --conf "$lake.client.region=us-east-1" \
  --conf "$lake.cache-enabled=false" \
  --conf spark.sql.session.timeZone=UTC \
  --conf spark.sql.shuffle.partitions=2 \
  --conf spark.sql.ansi.enabled=true \
  --conf spark.sql.hive.thriftServer.singleSession=true \
  --conf spark.ui.retainedJobs=50 \
  --conf spark.sql.ui.retainedExecutions=50 \
  --hiveconf hive.server2.thrift.port=10000 \
  --hiveconf hive.server2.thrift.bind.host=0.0.0.0 \
  --hiveconf hive.server2.authentication=NOSASL
