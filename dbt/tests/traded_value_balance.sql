-- 조인 fan-out 검사: Gold 대금 합과 Silver core 대금 합이 통화별로 같아야 합니다.
-- 통화가 다른 대금은 더하지 않으므로 통화마다 비교합니다.
-- 입고(TRANSFER_IN)는 마트에서 빠지므로 core 쪽도 뺍니다(mart_daily_symbol_trading과 같은 조건).
with core as (
  select currency, sum(exec_value) as v
  from {{ ref('fct_execution') }}
  where order_type is distinct from 'TRANSFER_IN'
  group by currency
),

gold as (
  select currency, sum(traded_value) as v
  from {{ ref('mart_daily_symbol_trading') }}
  group by currency
)

select
  coalesce(core.currency, gold.currency) as currency,
  core.v as core_value,
  gold.v as gold_value
from core
full outer join gold
  on core.currency = gold.currency
where core.v is null
   or gold.v is null
   or core.v <> gold.v
