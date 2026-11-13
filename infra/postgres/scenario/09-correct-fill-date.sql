-- 9단계: 지난 체결의 날짜 정정(u). 주문 2002와 체결 6002가 11-10이 아니라 11-09에 있었음.
-- 영향: 옛 거래일 (SPY, 11-10)은 체결이 0건이 되어 Gold에서 지워지고,
--       새 거래일 (SPY, 11-09)은 체결 2건으로 다시 묶입니다. 748.50달러는 11-09 저가와 고가 사이입니다.
BEGIN;
UPDATE source.orders
   SET ordered_at = '2026-11-09 15:10:00+00', updated_at = now()
 WHERE order_id = 2002;
UPDATE source.executions
   SET executed_at = '2026-11-09 15:10:05+00', updated_at = now()
 WHERE execution_id = 6002;
COMMIT;
