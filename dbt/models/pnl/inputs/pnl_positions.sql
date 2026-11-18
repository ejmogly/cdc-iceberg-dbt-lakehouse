-- 손익 모델의 입력 pnl_positions(계산 단위 = 계좌 x 종목). 체결이 있는 계좌와 종목마다 하나이고,
-- 기준일은 데이터의 마지막 달력 날, 기준 종목은 화면과 같은 SPY입니다.
select
  concat(e.account_id, ':', e.symbol) as position_id,
  e.symbol,
  (select max(cal_date) from {{ ref('pnl_calendar') }}) as asof_date,
  'SPY' as bench_symbol
from {{ ref('fct_execution') }} as e
group by e.account_id, e.symbol
