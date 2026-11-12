-- 첫 매매일부터 기준일까지 달력의 날마다 누적 손익(실현 + 평가, 원)과 고점 대비 하락(R14).
-- 그날의 매매를 모두 처리한 뒤 상태로 평가하고, 고점은 0에서 시작합니다(첫날 비싸게 산 손실도 셈).
-- 매도로 평가금액이 줄어도 실현 손익으로 옮겨 갈 뿐이라 손실로 세지 않습니다.
with last_of_day as (
  select
    r.position_id,
    r.calc_date,
    r.qty_after,
    r.cost_krw_after,
    sum(r.realized_krw) over (
      partition by r.position_id order by r.step
      rows between unbounded preceding and current row
    ) as realized_cum,
    row_number() over (partition by r.position_id, r.calc_date order by r.step desc) as rn
  from {{ ref('pnl_trade_result') }} r
),

state as (
  select
    position_id,
    calc_date as valid_from,
    lead(calc_date) over (partition by position_id order by calc_date) as valid_to,
    qty_after,
    cost_krw_after,
    realized_cum
  from last_of_day
  where rn = 1
),

valued as (
  select
    s.position_id,
    c.cal_date,
    s.realized_cum
      + case when s.qty_after = 0 then 0
             else s.qty_after * m.close_filled * (case when b.currency = 'USD' then m.usdkrw else 1 end)
        end
      - s.cost_krw_after as pnl_cum_krw
  from state s
  join {{ ref('pnl_position_base') }} b on b.position_id = s.position_id
  join {{ ref('pnl_calendar') }} c
    on c.cal_date >= s.valid_from
   and (s.valid_to is null or c.cal_date < s.valid_to)
   and c.cal_date <= b.end_date
  join {{ ref('pnl_market_daily') }} m
    on m.symbol = b.symbol and m.cal_date = c.cal_date
)

select
  position_id,
  cal_date,
  pnl_cum_krw,
  greatest(0, max(pnl_cum_krw) over (
    partition by position_id order by cal_date
    rows between unbounded preceding and current row
  )) as peak_krw
from valued
