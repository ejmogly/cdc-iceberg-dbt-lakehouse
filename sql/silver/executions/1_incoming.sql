-- Silver executions 1단계: Bronze 이력을 펼치고 검사한 뒤, 체결마다 최신 후보 하나를 고릅니다.
-- 규칙은 docs/decisions/0010, 뼈대는 학습 가이드 04 12-4입니다.
-- 2_merge.sql과 한 세션에서 이어 돌립니다(infra/spark/silver.sh).

SET spark.sql.ansi.enabled = true;

-- 한 행 = 체결 하나의 현재 상태. 철회(원천 삭제)는 is_deleted로 표시합니다.
CREATE TABLE IF NOT EXISTS lake.silver.executions (
  execution_id BIGINT,
  order_id     BIGINT,
  exec_qty     INT,
  exec_price   DECIMAL(18,4),
  fx_rate      DECIMAL(12,4),
  executed_at  TIMESTAMP,
  updated_at   TIMESTAMP,
  is_deleted   BOOLEAN,
  source_op    STRING,
  source_lsn   BIGINT,
  source_ts    TIMESTAMP,
  topic        STRING,
  partition_id INT,
  kafka_offset BIGINT
) USING iceberg
TBLPROPERTIES ('format-version' = '2');

-- executions만 REPLICA IDENTITY FULL이라(0005) 삭제의 before에 지워지기 전 값이 모두 있습니다.
-- 그래서 철회된 체결도 order_id, 수량, 가격을 남깁니다. 영향받는 주문과 날짜를 찾을 때 씁니다.
CREATE OR REPLACE TEMPORARY VIEW executions_changes AS
WITH envelopes AS (
  SELECT
    payload, topic, partition_id, kafka_offset,
    get_json_object(payload, '$.op') AS op,
    CAST(get_json_object(payload, '$.source.lsn') AS BIGINT) AS source_lsn,
    timestamp_millis(CAST(get_json_object(payload, '$.source.ts_ms') AS BIGINT)) AS source_ts,
    CASE WHEN get_json_object(payload, '$.op') = 'd'
      THEN get_json_object(payload, '$.before')
      ELSE get_json_object(payload, '$.after')
    END AS row_json
  FROM lake.bronze.executions
)
SELECT
  CAST(get_json_object(row_json, '$.execution_id') AS BIGINT) AS execution_id,
  CAST(get_json_object(row_json, '$.order_id') AS BIGINT) AS order_id,
  CAST(get_json_object(row_json, '$.exec_qty') AS INT) AS exec_qty,
  CAST(get_json_object(row_json, '$.exec_price') AS DECIMAL(18,4)) AS exec_price,
  CAST(get_json_object(row_json, '$.fx_rate') AS DECIMAL(12,4)) AS fx_rate,
  CAST(get_json_object(row_json, '$.executed_at') AS TIMESTAMP) AS executed_at,
  CAST(get_json_object(row_json, '$.updated_at') AS TIMESTAMP) AS updated_at,
  op = 'd' AS is_deleted,
  op AS source_op,
  source_lsn, source_ts, topic, partition_id, kafka_offset,
  payload
FROM envelopes;

SELECT assert_true(count(*) = 0,
  'executions: 키가 없거나, LSN이 없거나, 모르는 op인 Bronze 행이 있습니다') AS check_envelope
FROM executions_changes
WHERE execution_id IS NULL
   OR source_lsn IS NULL
   OR source_op IS NULL
   OR source_op NOT IN ('c', 'u', 'd', 'r');

SELECT assert_true(count(*) = 0,
  'executions: 같은 전달 위치(topic, partition, offset)에 다른 원문이 있습니다') AS check_position
FROM (
  SELECT topic, partition_id, kafka_offset
  FROM executions_changes
  GROUP BY topic, partition_id, kafka_offset
  HAVING count(DISTINCT payload) > 1
);

-- 삭제도 before에 진짜 값이 있으므로 모든 변경에서 검사합니다.
SELECT assert_true(count(*) = 0,
  'executions: 주문 번호, 수량, 가격, 체결 시각 가운데 빈 값이나 0 이하 수량, 가격이 있습니다') AS check_required
FROM executions_changes
WHERE order_id IS NULL OR executed_at IS NULL
   OR exec_qty IS NULL OR exec_qty <= 0
   OR exec_price IS NULL OR exec_price <= 0;

CREATE OR REPLACE TEMPORARY VIEW incoming_executions AS
SELECT
  execution_id, order_id, exec_qty, exec_price, fx_rate, executed_at, updated_at,
  is_deleted, source_op, source_lsn, source_ts, topic, partition_id, kafka_offset
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY execution_id
    ORDER BY source_lsn DESC, kafka_offset DESC
  ) AS rn
  FROM executions_changes
)
WHERE rn = 1;
