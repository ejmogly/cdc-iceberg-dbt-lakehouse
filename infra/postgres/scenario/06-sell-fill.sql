-- 6단계: 4주가 70,100원에 모두 체결되어 잔고 행을 지움
BEGIN;
INSERT INTO source.executions
  (execution_id, order_id, exec_qty, exec_price,
   fx_rate, executed_at, updated_at)
VALUES (5002, 1002, 4, 70100, NULL, now(), now());
UPDATE source.orders
   SET filled_qty = 4, status = 'FILLED',
       updated_at = now()
 WHERE order_id = 1002;
DELETE FROM source.holdings
 WHERE account_id = 'A-0001'
   AND symbol = '005930';
COMMIT;
