-- 4단계: 주문 1001의 남은 6주를 취소
UPDATE source.orders
   SET status = 'CANCELLED', updated_at = now()
 WHERE order_id = 1001;
