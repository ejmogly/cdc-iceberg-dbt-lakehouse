-- 1단계: 계좌 A-0001이 005930을 지정가 70,000원에 10주 매수 주문
INSERT INTO source.orders
  (order_id, account_id, symbol, side, order_type,
   order_qty, limit_price, filled_qty, status,
   ordered_at, updated_at)
VALUES
  (1001, 'A-0001', '005930', 'BUY', 'LIMIT',
   10, 70000, 0, 'ACCEPTED', now(), now());
