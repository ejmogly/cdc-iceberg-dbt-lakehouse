-- 이미 떠 있는 원천 DB의 주문 유형에 TRANSFER_IN(입고)을 더합니다(docs/decisions/0013).
-- 새로 만드는 DB는 init/10_brokerage.sql이 같은 제약을 만듭니다. 여러 번 돌려도 같습니다.
-- 이름 없이 만든 칸 제약은 PostgreSQL이 orders_order_type_check로 이름을 붙입니다(init에서는 이 이름을 직접 줌).
-- 제약은 DDL이라 Debezium(pgoutput)이 변경 이벤트를 내지 않고, 칸이 그대로라 Bronze와 Silver도 바뀌지 않습니다.
-- 바꾸는 동안 주문 테이블을 잠그지만, 행을 다시 쓰지 않고 남은 행이 새 제약에 맞는지만 봅니다.
BEGIN;
ALTER TABLE source.orders DROP CONSTRAINT IF EXISTS orders_order_type_check;
ALTER TABLE source.orders ADD CONSTRAINT orders_order_type_check
  CHECK (order_type IN ('LIMIT', 'MARKET', 'TRANSFER_IN'));
COMMIT;
