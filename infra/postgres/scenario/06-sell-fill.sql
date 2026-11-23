-- 6단계: 4주가 70,100원에 모두 체결되어 잔고 행을 지움
-- 체결 시각이 서울 날짜로 주말이면 dbt 테스트 no_weekend_trade_date가 실패하므로, 주말에 돌리면 직전 금요일 같은 시각으로 당깁니다.
\set now_weekday '(now() - make_interval(days => greatest(extract(isodow from now() at time zone ''Asia/Seoul'')::int - 5, 0)))'
BEGIN;
INSERT INTO source.executions
  (execution_id, order_id, exec_qty, exec_price,
   fx_rate, executed_at, updated_at)
VALUES (5002, 1002, 4, 70100, NULL, :now_weekday, :now_weekday);
UPDATE source.orders
   SET filled_qty = 4, status = 'FILLED',
       updated_at = :now_weekday
 WHERE order_id = 1002;
DELETE FROM source.holdings
 WHERE account_id = 'A-0001'
   AND symbol = '005930';
COMMIT;
