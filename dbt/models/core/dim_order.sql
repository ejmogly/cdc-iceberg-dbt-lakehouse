-- 주문 1건 = 1행. 계좌, 종목, 매수 매도 방향, 주문 수량, 상태.
-- 주문 상태로 체결을 거르지 않습니다. 취소된 주문의 이미 체결된 부분도 실제 거래입니다(가이드 05 12절 ③).
select
  order_id,
  account_id,
  symbol,
  side,
  order_type,
  order_qty,
  limit_price,
  filled_qty,
  status,
  ordered_at,
  updated_at
from {{ source('silver', 'orders') }}
where not is_deleted
