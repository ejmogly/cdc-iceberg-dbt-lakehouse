-- 과거 변경 다시 넣기: 이미 Silver에 반영된 옛 변경이 나중 배치에서 새 offset을 달고 다시 온 상황.
-- 1_incoming.sql 뒤, 2_merge.sql 앞에 끼워 incoming_* 뷰를 바꿔 치웁니다. Bronze와 Kafka는 건드리지 않습니다.
-- 기대: LSN이 더 작으므로 WHEN MATCHED의 버전 가드가 거절하고 Silver는 그대로입니다.
--   orders   주문 1001의 첫 접수(c, ACCEPTED, 70,000원)       → CANCELLED 유지
--   holdings 잔고 (A-0001, 005930)의 첫 매수(c, 4주, 69,500원) → 삭제 표시 유지
-- infra/spark/check-silver.sh가 이 순서로 돌립니다.

CREATE OR REPLACE TEMPORARY VIEW incoming_orders AS
SELECT
  order_id, account_id, symbol, side, order_type, order_qty, limit_price,
  filled_qty, status, ordered_at, updated_at,
  is_deleted, source_op, source_lsn, source_ts, topic, partition_id,
  kafka_offset + 1000 AS kafka_offset
FROM orders_changes
WHERE order_id = 1001 AND source_op = 'c';

CREATE OR REPLACE TEMPORARY VIEW incoming_holdings AS
SELECT
  account_id, symbol, qty, avg_price, updated_at,
  is_deleted, source_op, source_lsn, source_ts, topic, partition_id,
  kafka_offset + 1000 AS kafka_offset
FROM holdings_changes
WHERE account_id = 'A-0001' AND symbol = '005930' AND source_op = 'c';

-- 바꿔 친 입력이 정말 한 행씩인지 먼저 봅니다(0행이면 시험이 아무것도 하지 않음).
SELECT assert_true(count(*) = 1, '다시 넣을 주문 1001의 c가 한 행이 아님') AS redeliver_order
FROM incoming_orders;
SELECT assert_true(count(*) = 1, '다시 넣을 잔고 첫 매수 c가 한 행이 아님') AS redeliver_holding
FROM incoming_holdings;
