-- 잔고 대사. 계좌 x 종목 = 1행.
-- 체결로 쌓은 순수량(매수 - 매도)과 Silver 잔고의 수량을 나란히 두고 차이를 냅니다.
-- 전량 매도한 종목은 Silver에 삭제 표시만 남으므로 잔고 수량 0, 체결 순수량 0으로 맞습니다.
-- 전제: 원천이 빈 상태에서 시작해, 잔고가 모두 체결로만 생깁니다(첫 잔고를 따로 넣지 않음).
with from_executions as (
  select
    account_id,
    symbol,
    sum(case when side = 'BUY' then exec_qty else -exec_qty end) as executed_net_qty
  from {{ ref('fct_execution') }}
  group by account_id, symbol
),

from_holdings as (
  select account_id, symbol, qty as holding_qty
  from {{ source('silver', 'holdings') }}
  where not is_deleted
)

select
  coalesce(e.account_id, h.account_id) as account_id,
  coalesce(e.symbol, h.symbol) as symbol,
  coalesce(e.executed_net_qty, 0) as executed_net_qty,
  coalesce(h.holding_qty, 0) as holding_qty,
  coalesce(h.holding_qty, 0) - coalesce(e.executed_net_qty, 0) as qty_diff
from from_executions as e
full outer join from_holdings as h
  on e.account_id = h.account_id
  and e.symbol = h.symbol
