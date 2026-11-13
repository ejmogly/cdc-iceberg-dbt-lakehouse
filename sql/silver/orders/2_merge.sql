-- Silver orders 2단계: 최신 후보(incoming_orders)를 현재 상태 테이블에 반영합니다.
-- 뼈대는 docs/guide/04-bronze-to-silver.md 12-5입니다. 1_incoming.sql과 한 세션에서 돌립니다.
-- ON에는 "같은 주문인가"만 두고, 버전 가드는 WHEN MATCHED에 둡니다.
-- 가드를 ON에 넣으면 늦게 온 옛 변경이 "없는 키"로 보여 INSERT로 빠집니다.
-- UPDATE SET *, INSERT *는 같은 이름의 칸끼리 맞춥니다(incoming_orders의 칸 = 테이블의 칸).
MERGE INTO lake.silver.orders AS t
USING incoming_orders AS s
ON t.order_id = s.order_id
WHEN MATCHED AND (
  s.source_lsn > t.source_lsn
  OR (s.source_lsn = t.source_lsn AND s.kafka_offset > t.kafka_offset)
) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
