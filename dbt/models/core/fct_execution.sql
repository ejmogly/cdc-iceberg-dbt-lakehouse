-- 체결 1건 = 1행. 철회된 체결을 빼고, 주문을 거쳐 계좌, 종목, 방향을 붙이고,
-- 거래일을 그 시장의 현지 날짜로 정합니다(가이드 05 12절 ②, ③).
-- 조인은 모두 left join이라 짝이 없으면 행이 사라지지 않고 칸이 비고, not_null 테스트가 잡습니다.
-- 세션 시간대가 UTC라(infra/spark/start-thrift.sh) executed_at은 UTC 시각입니다.
with executions as (
  select *
  from {{ source('silver', 'executions') }}
  where not is_deleted
),

orders as (
  select order_id, account_id, symbol, side, order_type
  from {{ ref('dim_order') }}
),

instruments as (
  select symbol, market, currency
  from {{ ref('instruments') }}
),

markets as (
  select market, tz
  from {{ ref('markets') }}
)

select
  e.execution_id,
  e.order_id,
  o.account_id,
  o.symbol,
  i.market,
  i.currency,
  o.side,
  o.order_type,
  e.exec_qty,
  e.exec_price,
  e.exec_qty * e.exec_price as exec_value,
  e.fx_rate,
  e.executed_at,
  to_date(from_utc_timestamp(e.executed_at, m.tz)) as trade_date
from executions as e
left join orders as o
  on e.order_id = o.order_id
left join instruments as i
  on o.symbol = i.symbol
left join markets as m
  on i.market = m.market
