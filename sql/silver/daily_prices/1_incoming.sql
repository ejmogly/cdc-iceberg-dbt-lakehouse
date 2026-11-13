-- Silver daily_prices 1단계: Bronze 이력을 펼치고 검사한 뒤, 종목과 거래일마다 최신 후보 하나를 고릅니다.
-- 규칙은 docs/decisions/0010입니다. 2_merge.sql과 한 세션에서 이어 돌립니다(infra/spark/silver.sh).

SET spark.sql.ansi.enabled = true;

-- 한 행 = 종목 하나의 거래일 하루 시세(최신 값). 제공처가 지난 값을 고치면 u로 옵니다.
CREATE TABLE IF NOT EXISTS lake.silver.daily_prices (
  symbol       STRING,
  trade_date   DATE,
  open_price   DECIMAL(18,4),
  high_price   DECIMAL(18,4),
  low_price    DECIMAL(18,4),
  close_price  DECIMAL(18,4),
  volume       BIGINT,
  provider     STRING,
  loaded_at    TIMESTAMP,
  is_deleted   BOOLEAN,
  source_op    STRING,
  source_lsn   BIGINT,
  source_ts    TIMESTAMP,
  topic        STRING,
  partition_id INT,
  kafka_offset BIGINT
) USING iceberg
TBLPROPERTIES ('format-version' = '2');

-- trade_date는 Debezium이 1970-01-01부터 센 날 수(정수)로 보냅니다(0004 3번).
-- REPLICA IDENTITY DEFAULT라 삭제의 before에서 키 밖 칸은 채움 값이므로 비웁니다.
CREATE OR REPLACE TEMPORARY VIEW daily_prices_changes AS
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
  FROM lake.bronze.daily_prices
)
SELECT
  get_json_object(row_json, '$.symbol') AS symbol,
  date_add(DATE '1970-01-01', CAST(get_json_object(row_json, '$.trade_date') AS INT)) AS trade_date,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.open_price') AS DECIMAL(18,4)) END AS open_price,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.high_price') AS DECIMAL(18,4)) END AS high_price,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.low_price') AS DECIMAL(18,4)) END AS low_price,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.close_price') AS DECIMAL(18,4)) END AS close_price,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.volume') AS BIGINT) END AS volume,
  CASE WHEN op <> 'd' THEN get_json_object(row_json, '$.provider') END AS provider,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.loaded_at') AS TIMESTAMP) END AS loaded_at,
  op = 'd' AS is_deleted,
  op AS source_op,
  source_lsn, source_ts, topic, partition_id, kafka_offset,
  payload
FROM envelopes;

SELECT assert_true(count(*) = 0,
  'daily_prices: 키 두 칸 가운데 하나가 없거나, LSN이 없거나, 모르는 op인 Bronze 행이 있습니다') AS check_envelope
FROM daily_prices_changes
WHERE symbol IS NULL
   OR trade_date IS NULL
   OR source_lsn IS NULL
   OR source_op IS NULL
   OR source_op NOT IN ('c', 'u', 'd', 'r');

SELECT assert_true(count(*) = 0,
  'daily_prices: 같은 전달 위치(topic, partition, offset)에 다른 원문이 있습니다') AS check_position
FROM (
  SELECT topic, partition_id, kafka_offset
  FROM daily_prices_changes
  GROUP BY topic, partition_id, kafka_offset
  HAVING count(DISTINCT payload) > 1
);

SELECT assert_true(count(*) = 0,
  'daily_prices: 삭제가 아닌 변경에 가격이나 거래량이 없거나, 저가와 고가 범위가 맞지 않습니다') AS check_required
FROM daily_prices_changes
WHERE NOT is_deleted
  AND (open_price IS NULL OR high_price IS NULL OR low_price IS NULL
       OR close_price IS NULL OR volume IS NULL
       OR low_price > high_price
       OR close_price NOT BETWEEN low_price AND high_price);

CREATE OR REPLACE TEMPORARY VIEW incoming_daily_prices AS
SELECT
  symbol, trade_date, open_price, high_price, low_price, close_price, volume,
  provider, loaded_at,
  is_deleted, source_op, source_lsn, source_ts, topic, partition_id, kafka_offset
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY symbol, trade_date
    ORDER BY source_lsn DESC, kafka_offset DESC
  ) AS rn
  FROM daily_prices_changes
)
WHERE rn = 1;
