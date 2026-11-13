-- 3단계: 4주 체결. 체결, 주문, 잔고를 한 트랜잭션으로 바꿈
BEGIN;
INSERT INTO source.executions
  (execution_id, order_id, exec_qty, exec_price,
   fx_rate, executed_at, updated_at)
VALUES (5001, 1001, 4, 69500, NULL, now(), now());
UPDATE source.orders
   SET filled_qty = 4, status = 'PARTIAL',
       updated_at = now()
 WHERE order_id = 1001;
INSERT INTO source.holdings
  (account_id, symbol, qty, avg_price, updated_at)
VALUES ('A-0001', '005930', 4, 69500, now());
COMMIT;
