-- Silver holdings 1단계: Bronze 이력을 펼치고 검사한 뒤, 잔고마다 최신 후보 하나를 고릅니다.
-- 규칙은 docs/decisions/0010, 뼈대는 학습 가이드 04 12-4입니다.
-- 2_merge.sql과 한 세션에서 이어 돌립니다(infra/spark/silver.sh).

SET spark.sql.ansi.enabled = true;

-- 한 행 = 계좌 하나가 가진 종목 하나의 현재 보유. 전량 매도로 원천 행이 지워져도
-- 행을 남기고 is_deleted = true, qty와 avg_price는 NULL로 둡니다(0010 3, 4번).
-- 유효한 잔고는 WHERE NOT is_deleted로 읽습니다.
CREATE TABLE IF NOT EXISTS lake.silver.holdings (
  account_id   STRING,
  symbol       STRING,
  qty          INT,
  avg_price    DECIMAL(18,4),
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

-- holdings는 REPLICA IDENTITY DEFAULT라 삭제의 before에는 키만 진짜 값이고,
-- qty 0, avg_price "0.0000" 같은 채움 값이 옵니다. "0주 보유"로 읽히지 않게 비웁니다.
CREATE OR REPLACE TEMPORARY VIEW holdings_changes AS
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
  FROM lake.bronze.holdings
)
SELECT
  get_json_object(row_json, '$.account_id') AS account_id,
  get_json_object(row_json, '$.symbol') AS symbol,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.qty') AS INT) END AS qty,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.avg_price') AS DECIMAL(18,4)) END AS avg_price,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.updated_at') AS TIMESTAMP) END AS updated_at,
  op = 'd' AS is_deleted,
  op AS source_op,
  source_lsn, source_ts, topic, partition_id, kafka_offset,
  payload
FROM envelopes;

SELECT assert_true(count(*) = 0,
  'holdings: 키 두 칸 가운데 하나가 없거나, LSN이 없거나, 모르는 op인 Bronze 행이 있습니다') AS check_envelope
FROM holdings_changes
WHERE account_id IS NULL
   OR symbol IS NULL
   OR source_lsn IS NULL
   OR source_op IS NULL
   OR source_op NOT IN ('c', 'u', 'd', 'r');

SELECT assert_true(count(*) = 0,
  'holdings: 같은 전달 위치(topic, partition, offset)에 다른 원문이 있습니다') AS check_position
FROM (
  SELECT topic, partition_id, kafka_offset
  FROM holdings_changes
  GROUP BY topic, partition_id, kafka_offset
  HAVING count(DISTINCT payload) > 1
);

-- 수량 검사는 삭제 행을 빼고 합니다(삭제 행의 수량은 위에서 비웠음).
SELECT assert_true(count(*) = 0,
  'holdings: 삭제가 아닌 변경에 수량이나 평균 단가가 없거나 수량이 0 이하입니다') AS check_required
FROM holdings_changes
WHERE NOT is_deleted
  AND (qty IS NULL OR qty <= 0 OR avg_price IS NULL);

-- 키 두 칸을 모두 PARTITION BY에 넣습니다. 삭제도 후보에서 빼지 않습니다.
CREATE OR REPLACE TEMPORARY VIEW incoming_holdings AS
SELECT
  account_id, symbol, qty, avg_price, updated_at,
  is_deleted, source_op, source_lsn, source_ts, topic, partition_id, kafka_offset
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY account_id, symbol
    ORDER BY source_lsn DESC, kafka_offset DESC
  ) AS rn
  FROM holdings_changes
)
WHERE rn = 1;
