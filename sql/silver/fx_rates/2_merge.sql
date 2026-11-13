-- Silver fx_rates 2단계: 최신 후보(incoming_fx_rates)를 현재 상태 테이블에 반영합니다.
-- 키는 (currency_pair, rate_date)입니다. 버전 가드는 WHEN MATCHED에 둡니다.
MERGE INTO lake.silver.fx_rates AS t
USING incoming_fx_rates AS s
ON t.currency_pair = s.currency_pair
  AND t.rate_date = s.rate_date
WHEN MATCHED AND (
  s.source_lsn > t.source_lsn
  OR (s.source_lsn = t.source_lsn AND s.kafka_offset > t.kafka_offset)
) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
