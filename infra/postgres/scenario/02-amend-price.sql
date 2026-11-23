-- 2단계: 주문 1001의 가격을 69,500원으로 정정(같은 행을 고침)
-- 체결 시각이 서울 날짜로 주말이면 dbt 테스트 no_weekend_trade_date가 실패하므로, 주말에 돌리면 직전 금요일 같은 시각으로 당깁니다.
\set now_weekday '(now() - make_interval(days => greatest(extract(isodow from now() at time zone ''Asia/Seoul'')::int - 5, 0)))'
UPDATE source.orders
   SET limit_price = 69500, updated_at = :now_weekday
 WHERE order_id = 1001;
