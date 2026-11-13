-- Silver executions 2단계: 최신 후보(incoming_executions)를 현재 상태 테이블에 반영합니다.
-- orders/2_merge.sql과 같은 규칙입니다. 버전 가드는 WHEN MATCHED에 둡니다.
MERGE INTO lake.silver.executions AS t
USING incoming_executions AS s
ON t.execution_id = s.execution_id
WHEN MATCHED AND (
  s.source_lsn > t.source_lsn
  OR (s.source_lsn = t.source_lsn AND s.kafka_offset > t.kafka_offset)
) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
