-- 5단계: 보유한 4주를 시장가로 매도 주문
-- 체결 시각이 서울 날짜로 주말이면 dbt 테스트 no_weekend_trade_date가 실패하므로, 주말에 돌리면 직전 금요일 같은 시각으로 당깁니다.
\set now_weekday '(now() - make_interval(days => greatest(extract(isodow from now() at time zone ''Asia/Seoul'')::int - 5, 0)))'
INSERT INTO source.orders
  (order_id, account_id, symbol, side, order_type,
   order_qty, limit_price, filled_qty, status,
   ordered_at, updated_at)
VALUES
  (1002, 'A-0001', '005930', 'SELL', 'MARKET',
   4, NULL, 0, 'ACCEPTED', :now_weekday, :now_weekday);
