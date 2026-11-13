-- Kafka 토픽 다섯 개를 Iceberg Bronze 테이블 다섯 개로 쌓는 Flink SQL 작업.
-- 직접 돌리지 않고 infra/flink/submit-bronze.sh가 @이름@ 자리에 컨테이너 환경 변수를 넣어 보냅니다.
-- 설계: docs/design/layers.md 1절. 버전: docs/decisions/0002.

SET 'execution.target' = 'remote';
SET 'rest.address' = 'jobmanager';
SET 'rest.port' = '8081';
SET 'pipeline.name' = 'bronze-brokerage';
SET 'parallelism.default' = '1';
SET 'execution.checkpointing.interval' = '@CHECKPOINT_INTERVAL@';
SET 'execution.checkpointing.mode' = 'EXACTLY_ONCE';
SET 'table.local-time-zone' = 'UTC';

-- Iceberg 카탈로그: 테이블 목록은 PostgreSQL(catalog-db), 파일은 S3 호환 저장소에 둡니다.
CREATE CATALOG lake WITH (
  'type' = 'iceberg',
  'catalog-impl' = 'org.apache.iceberg.jdbc.JdbcCatalog',
  'uri' = 'jdbc:postgresql://catalog-db:5432/@CATALOG_DB@',
  'jdbc.user' = '@CATALOG_USER@',
  'jdbc.password' = '@CATALOG_PASSWORD@',
  'warehouse' = 's3://@S3_BUCKET@/',
  'io-impl' = 'org.apache.iceberg.aws.s3.S3FileIO',
  's3.endpoint' = 'http://s3:9000',
  's3.path-style-access' = 'true',
  'client.region' = 'us-east-1'
);

-- default는 Spark Thrift 세션이 처음 여는 이름공간이라 함께 만듭니다.
CREATE DATABASE IF NOT EXISTS lake.`default`;
CREATE DATABASE IF NOT EXISTS lake.bronze;
CREATE DATABASE IF NOT EXISTS lake.silver;
CREATE DATABASE IF NOT EXISTS lake.gold;

-- 원천 테이블 하나에 Bronze 테이블 하나. 칸은 넷 모두 같습니다.
--   payload      Debezium이 보낸 JSON 원문(before, after, source, op)
--   topic, partition_id, kafka_offset  Kafka에서의 전달 위치. 중복을 가리는 키
--   kafka_ts     Kafka 레코드 시각(Connect가 보낸 시각)
--   ingested_at  Flink가 이 행을 만든 시각. 실제 commit은 다음 checkpoint 때
CREATE TABLE IF NOT EXISTS lake.bronze.orders (
  payload      STRING,
  topic        STRING,
  partition_id INT,
  kafka_offset BIGINT,
  kafka_ts     TIMESTAMP_LTZ(3),
  ingested_at  TIMESTAMP_LTZ(3)
) WITH ('format-version' = '2');

CREATE TABLE IF NOT EXISTS lake.bronze.executions (
  payload      STRING,
  topic        STRING,
  partition_id INT,
  kafka_offset BIGINT,
  kafka_ts     TIMESTAMP_LTZ(3),
  ingested_at  TIMESTAMP_LTZ(3)
) WITH ('format-version' = '2');

CREATE TABLE IF NOT EXISTS lake.bronze.holdings (
  payload      STRING,
  topic        STRING,
  partition_id INT,
  kafka_offset BIGINT,
  kafka_ts     TIMESTAMP_LTZ(3),
  ingested_at  TIMESTAMP_LTZ(3)
) WITH ('format-version' = '2');

CREATE TABLE IF NOT EXISTS lake.bronze.daily_prices (
  payload      STRING,
  topic        STRING,
  partition_id INT,
  kafka_offset BIGINT,
  kafka_ts     TIMESTAMP_LTZ(3),
  ingested_at  TIMESTAMP_LTZ(3)
) WITH ('format-version' = '2');

-- 환율(원천 20_fx_rates.sql). 2026-11-12에 더함
CREATE TABLE IF NOT EXISTS lake.bronze.fx_rates (
  payload      STRING,
  topic        STRING,
  partition_id INT,
  kafka_offset BIGINT,
  kafka_ts     TIMESTAMP_LTZ(3),
  ingested_at  TIMESTAMP_LTZ(3)
) WITH ('format-version' = '2');

-- Kafka 읽기 하나로 토픽 다섯 개를 받습니다.
-- 토픽 목록 대신 패턴을 쓰는 이유: 아직 행이 없어 토픽이 없는 테이블이 있어도
-- 작업이 실패하지 않고, 토픽이 생기면 5초 안에 찾아 처음부터 읽습니다.
-- 패턴에 없는 토픽(뉴스, 관심도 등)은 읽지 않습니다.
-- 패턴이나 INSERT를 바꾼 뒤에는 작업 모양이 달라지므로 savepoint 없이 다시 보냅니다(submit-bronze.sh).
-- 그래도 이미 읽은 토픽은 Kafka에 commit된 위치부터, 새 토픽은 처음부터 읽습니다.
-- group-offsets: checkpoint가 끝날 때 commit한 위치부터 다시 읽고, commit이 없으면 earliest.
-- format raw: 메시지 값을 해석하지 않고 글자 그대로 받습니다. 값이 없는 tombstone은 행이 되지 않고,
-- 아래 WHERE가 한 번 더 거릅니다. 삭제(op = d) 메시지는 값이 있으므로 남습니다.
CREATE TEMPORARY TABLE kafka_cdc (
  payload      STRING,
  topic        STRING           METADATA FROM 'topic'     VIRTUAL,
  partition_id INT              METADATA FROM 'partition' VIRTUAL,
  kafka_offset BIGINT           METADATA FROM 'offset'    VIRTUAL,
  kafka_ts     TIMESTAMP_LTZ(3) METADATA FROM 'timestamp' VIRTUAL
) WITH (
  'connector' = 'kafka',
  'topic-pattern' = 'src\.source\.(orders|executions|holdings|daily_prices|fx_rates)',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'flink-bronze-brokerage',
  'properties.auto.offset.reset' = 'earliest',
  'scan.startup.mode' = 'group-offsets',
  'scan.topic-partition-discovery.interval' = '5s',
  'format' = 'raw'
);

EXECUTE STATEMENT SET
BEGIN
  INSERT INTO lake.bronze.orders
  SELECT payload, topic, partition_id, kafka_offset, kafka_ts, CURRENT_TIMESTAMP
  FROM kafka_cdc
  WHERE payload IS NOT NULL AND topic = 'src.source.orders';

  INSERT INTO lake.bronze.executions
  SELECT payload, topic, partition_id, kafka_offset, kafka_ts, CURRENT_TIMESTAMP
  FROM kafka_cdc
  WHERE payload IS NOT NULL AND topic = 'src.source.executions';

  INSERT INTO lake.bronze.holdings
  SELECT payload, topic, partition_id, kafka_offset, kafka_ts, CURRENT_TIMESTAMP
  FROM kafka_cdc
  WHERE payload IS NOT NULL AND topic = 'src.source.holdings';

  INSERT INTO lake.bronze.daily_prices
  SELECT payload, topic, partition_id, kafka_offset, kafka_ts, CURRENT_TIMESTAMP
  FROM kafka_cdc
  WHERE payload IS NOT NULL AND topic = 'src.source.daily_prices';

  INSERT INTO lake.bronze.fx_rates
  SELECT payload, topic, partition_id, kafka_offset, kafka_ts, CURRENT_TIMESTAMP
  FROM kafka_cdc
  WHERE payload IS NOT NULL AND topic = 'src.source.fx_rates';
END;
