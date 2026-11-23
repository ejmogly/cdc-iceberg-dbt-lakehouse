-- 1단계: 계좌 A-0001이 005930을 지정가 70,000원에 10주 매수 주문
-- 체결 시각이 서울 날짜로 주말이면 dbt 테스트 no_weekend_trade_date가 실패하므로, 주말에 돌리면 직전 금요일 같은 시각으로 당깁니다.
\set now_weekday '(now() - make_interval(days => greatest(extract(isodow from now() at time zone ''Asia/Seoul'')::int - 5, 0)))'
INSERT INTO source.orders
  (order_id, account_id, symbol, side, order_type,
   order_qty, limit_price, filled_qty, status,
   ordered_at, updated_at)
VALUES
  (1001, 'A-0001', '005930', 'BUY', 'LIMIT',
   10, 70000, 0, 'ACCEPTED', :now_weekday, :now_weekday);
