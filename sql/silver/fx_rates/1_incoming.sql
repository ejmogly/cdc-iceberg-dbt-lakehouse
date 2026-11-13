-- Silver fx_rates 1단계: Bronze 이력을 펼치고 검사한 뒤, 통화쌍과 고시일마다 최신 후보 하나를 고릅니다.
-- 규칙은 docs/decisions/0010, 환율 출처와 뜻은 0007입니다. 2_merge.sql과 한 세션에서 이어 돌립니다(infra/spark/silver.sh).

SET spark.sql.ansi.enabled = true;

-- 한 행 = 통화쌍 하나의 고시일 하루 환율(최신 값). 한국 휴일에는 행이 없습니다.
-- rate_date는 고시한 나라의 날짜입니다. 미국 거래일과 맞추는 규칙은 Gold에서 정합니다(0007).
CREATE TABLE IF NOT EXISTS lake.silver.fx_rates (
  currency_pair STRING,
  rate_date     DATE,
  rate          DECIMAL(12,4),
  provider      STRING,
  loaded_at     TIMESTAMP,
  is_deleted    BOOLEAN,
  source_op     STRING,
  source_lsn    BIGINT,
  source_ts     TIMESTAMP,
  topic         STRING,
  partition_id  INT,
  kafka_offset  BIGINT
) USING iceberg
TBLPROPERTIES ('format-version' = '2');

-- rate_date는 Debezium이 1970-01-01부터 센 날 수(정수)로 보냅니다(0004 3번).
-- REPLICA IDENTITY DEFAULT라 삭제의 before에서 키 밖 칸은 채움 값이므로 비웁니다.
CREATE OR REPLACE TEMPORARY VIEW fx_rates_changes AS
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
  FROM lake.bronze.fx_rates
)
SELECT
  get_json_object(row_json, '$.currency_pair') AS currency_pair,
  date_add(DATE '1970-01-01', CAST(get_json_object(row_json, '$.rate_date') AS INT)) AS rate_date,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.rate') AS DECIMAL(12,4)) END AS rate,
  CASE WHEN op <> 'd' THEN get_json_object(row_json, '$.provider') END AS provider,
  CASE WHEN op <> 'd' THEN CAST(get_json_object(row_json, '$.loaded_at') AS TIMESTAMP) END AS loaded_at,
  op = 'd' AS is_deleted,
  op AS source_op,
  source_lsn, source_ts, topic, partition_id, kafka_offset,
  payload
FROM envelopes;

SELECT assert_true(count(*) = 0,
  'fx_rates: 키 두 칸 가운데 하나가 없거나, LSN이 없거나, 모르는 op인 Bronze 행이 있습니다') AS check_envelope
FROM fx_rates_changes
WHERE currency_pair IS NULL
   OR rate_date IS NULL
   OR source_lsn IS NULL
   OR source_op IS NULL
   OR source_op NOT IN ('c', 'u', 'd', 'r');

SELECT assert_true(count(*) = 0,
  'fx_rates: 같은 전달 위치(topic, partition, offset)에 다른 원문이 있습니다') AS check_position
FROM (
  SELECT topic, partition_id, kafka_offset
  FROM fx_rates_changes
  GROUP BY topic, partition_id, kafka_offset
  HAVING count(DISTINCT payload) > 1
);

SELECT assert_true(count(*) = 0,
  'fx_rates: 삭제가 아닌 변경에 환율이 없거나 0 이하입니다') AS check_required
FROM fx_rates_changes
WHERE NOT is_deleted
  AND (rate IS NULL OR rate <= 0);

CREATE OR REPLACE TEMPORARY VIEW incoming_fx_rates AS
SELECT
  currency_pair, rate_date, rate, provider, loaded_at,
  is_deleted, source_op, source_lsn, source_ts, topic, partition_id, kafka_offset
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY currency_pair, rate_date
    ORDER BY source_lsn DESC, kafka_offset DESC
  ) AS rn
  FROM fx_rates_changes
)
WHERE rn = 1;
