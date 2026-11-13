-- 2단계: 주문 1001의 가격을 69,500원으로 정정(같은 행을 고침)
UPDATE source.orders
   SET limit_price = 69500, updated_at = now()
 WHERE order_id = 1001;
