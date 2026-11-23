-- 3단계: 4주 체결. 체결, 주문, 잔고를 한 트랜잭션으로 바꿈
-- 체결 시각이 서울 날짜로 주말이면 dbt 테스트 no_weekend_trade_date가 실패하므로, 주말에 돌리면 직전 금요일 같은 시각으로 당깁니다.
\set now_weekday '(now() - make_interval(days => greatest(extract(isodow from now() at time zone ''Asia/Seoul'')::int - 5, 0)))'
BEGIN;
INSERT INTO source.executions
  (execution_id, order_id, exec_qty, exec_price,
   fx_rate, executed_at, updated_at)
VALUES (5001, 1001, 4, 69500, NULL, :now_weekday, :now_weekday);
UPDATE source.orders
   SET filled_qty = 4, status = 'PARTIAL',
       updated_at = :now_weekday
 WHERE order_id = 1001;
INSERT INTO source.holdings
  (account_id, symbol, qty, avg_price, updated_at)
VALUES ('A-0001', '005930', 4, 69500, :now_weekday);
COMMIT;
