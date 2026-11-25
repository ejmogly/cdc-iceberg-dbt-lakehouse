-- Silver orders 1단계: Bronze 이력을 펼치고 검사한 뒤, 주문마다 최신 후보 하나를 고릅니다.
-- 규칙은 docs/decisions/0010, 뼈대는 학습 가이드 04 12-4입니다.
-- 2_merge.sql과 한 세션에서 이어 돌립니다(infra/spark/silver.sh).

-- 글자를 숫자나 시각으로 바꾸다 실패하면 NULL로 넘기지 않고 오류로 멈춥니다.
SET spark.sql.ansi.enabled = true;

-- 한 행 = 주문 하나의 현재 상태. 주문은 지우지 않고 취소도 status로 표현하지만,
-- 원천에서 행이 지워지면 is_deleted로 표시합니다.
CREATE TABLE IF NOT EXISTS lake.silver.orders (
  order_id     BIGINT,
  account_id   STRING,
  symbol       STRING,
  side         STRING,
  order_type   STRING,
  order_qty    INT,
  limit_price  DECIMAL(18,4),
  filled_qty   INT,
  status       STRING,
  ordered_at   TIMESTAMP,
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

-- Bronze 한 행 = 변경 하나. 삭제(d)는 before를, 나머지(c, u, r)는 after를 읽습니다.
-- orders는 REPLICA IDENTITY DEFAULT라 삭제의 before에서 키 밖 칸은 채움 값입니다. 그래서 비웁니다.
CREATE OR REPLACE TEMPORARY VIEW orders_changes AS
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
  FROM lake.bronze.orders
)
SELECT
  CAST(get_json_object(row_json, '$.order_id') AS BIGINT) AS order_id,
  CASE WHEN op <> 'd' THEN get_json_object(row_json, '$.account_id') END AS account_id,
  CASE WHEN op <> 'd' THEN get_json_object(row_json, '$.symbol') END AS symbol,
  CASE WHEN op <> 'd' THEN get_json_object(row_json, '$.side') END AS side,
  CASE WHEN op <> 'd' THEN get_json_object(row_json, '$.order_type') END AS order_type,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.order_qty') AS INT) END AS order_qty,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.limit_price') AS DECIMAL(18,4)) END AS limit_price,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.filled_qty') AS INT) END AS filled_qty,
  CASE WHEN op <> 'd' THEN get_json_object(row_json, '$.status') END AS status,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.ordered_at') AS TIMESTAMP) END AS ordered_at,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.updated_at') AS TIMESTAMP) END AS updated_at,
  op = 'd' AS is_deleted,
  op AS source_op,
  source_lsn, source_ts, topic, partition_id, kafka_offset,
  payload
FROM envelopes;

-- 입력 검사. 하나라도 걸리면 assert_true가 오류를 내고 MERGE까지 가지 않습니다.
SELECT assert_true(count(*) = 0,
  'orders: 키가 없거나, LSN이 없거나, 모르는 op인 Bronze 행이 있습니다') AS check_envelope
FROM orders_changes
WHERE order_id IS NULL
   OR source_lsn IS NULL
   OR source_op IS NULL
   OR source_op NOT IN ('c', 'u', 'd', 'r');

SELECT assert_true(count(*) = 0,
  'orders: 같은 전달 위치(topic, partition, offset)에 다른 원문이 있습니다') AS check_position
FROM (
  SELECT topic, partition_id, kafka_offset
  FROM orders_changes
  GROUP BY topic, partition_id, kafka_offset
  HAVING count(DISTINCT payload) > 1
);

SELECT assert_true(count(*) = 0,
  'orders: 삭제가 아닌 변경에 꼭 있어야 할 값이 비었습니다') AS check_required
FROM orders_changes
WHERE NOT is_deleted
  AND (account_id IS NULL OR symbol IS NULL OR side IS NULL
       OR order_qty IS NULL OR filled_qty IS NULL OR status IS NULL);

-- 주문마다 최신 후보 하나. LSN을 먼저, 같으면 offset을 봅니다. 삭제도 후보에서 빼지 않습니다.
CREATE OR REPLACE TEMPORARY VIEW incoming_orders AS
SELECT
  order_id, account_id, symbol, side, order_type, order_qty, limit_price,
  filled_qty, status, ordered_at, updated_at,
  is_deleted, source_op, source_lsn, source_ts, topic, partition_id, kafka_offset
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY order_id
    ORDER BY source_lsn DESC, kafka_offset DESC
  ) AS rn
  FROM orders_changes
)
WHERE rn = 1;
