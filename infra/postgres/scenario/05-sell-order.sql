-- 5단계: 보유한 4주를 시장가로 매도 주문
INSERT INTO source.orders
  (order_id, account_id, symbol, side, order_type,
   order_qty, limit_price, filled_qty, status,
   ordered_at, updated_at)
VALUES
  (1002, 'A-0001', '005930', 'SELL', 'MARKET',
   4, NULL, 0, 'ACCEPTED', now(), now());
