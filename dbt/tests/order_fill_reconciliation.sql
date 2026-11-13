-- 주문별 체결 수량 대사: 철회되지 않은 체결 수량의 합이 주문의 filled_qty와 같고,
-- 주문 수량을 넘지 않아야 합니다. 체결이 없는 주문은 filled_qty가 0이어야 합니다.
with fills as (
  select order_id, sum(exec_qty) as executed_qty
  from {{ ref('fct_execution') }}
  group by order_id
)

select
  o.order_id,
  o.order_qty,
  o.filled_qty,
  coalesce(f.executed_qty, 0) as executed_qty
from {{ ref('dim_order') }} as o
left join fills as f
  on o.order_id = f.order_id
where coalesce(f.executed_qty, 0) <> o.filled_qty
   or coalesce(f.executed_qty, 0) > o.order_qty
