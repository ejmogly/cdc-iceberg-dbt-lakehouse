-- Silver daily_prices 2단계: 최신 후보(incoming_daily_prices)를 현재 상태 테이블에 반영합니다.
-- 키는 (symbol, trade_date)입니다. 버전 가드는 WHEN MATCHED에 둡니다.
MERGE INTO lake.silver.daily_prices AS t
USING incoming_daily_prices AS s
ON t.symbol = s.symbol
  AND t.trade_date = s.trade_date
WHEN MATCHED AND (
  s.source_lsn > t.source_lsn
  OR (s.source_lsn = t.source_lsn AND s.kafka_offset > t.kafka_offset)
) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
