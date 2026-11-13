-- Silver holdings 2단계: 최신 후보(incoming_holdings)를 현재 상태 테이블에 반영합니다.
-- ON의 AND는 키 두 칸이 모두 같은지("같은 잔고인가")만 묻습니다. 버전 가드는 WHEN MATCHED에 둡니다.
-- 전량 매도 뒤 다시 사면 같은 키의 c가 더 큰 LSN으로 와서 가드를 통과하고 is_deleted = false가 됩니다.
MERGE INTO lake.silver.holdings AS t
USING incoming_holdings AS s
ON t.account_id = s.account_id
  AND t.symbol = s.symbol
WHEN MATCHED AND (
  s.source_lsn > t.source_lsn
  OR (s.source_lsn = t.source_lsn AND s.kafka_offset > t.kafka_offset)
) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
