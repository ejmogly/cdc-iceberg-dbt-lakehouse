-- 기준일의 상태: 장부 마지막 줄의 수량과 원가, 기준일의 종가와 환율(없으면 직전 값, R09).
-- 매매가 하나도 남지 않은 계산 단위는 0으로 둡니다.
with last_step as (
  select
    l.*,
    row_number() over (partition by l.position_id order by l.step desc) as rn
  from {{ ref('pnl_ledger') }} l
)

select
  b.position_id,
  b.symbol,
  b.currency,
  b.end_date,
  coalesce(ls.qty_after, 0) as qty_end,
  coalesce(ls.cost_local_after, cast(0 as decimal(38, 18))) as cost_local_end,
  coalesce(ls.cost_krw_after, cast(0 as decimal(38, 18))) as cost_krw_end,
  coalesce(ls.bench_units_after, cast(0 as decimal(38, 18))) as bench_units_end,
  m.close_filled as px_end,
  case when b.currency = 'USD' then m.usdkrw else 1 end as fx_end,
  bm.close_filled * bm.usdkrw as bench_px_krw_end
from {{ ref('pnl_position_base') }} b
left join last_step ls on ls.position_id = b.position_id and ls.rn = 1
join {{ ref('pnl_market_daily') }} m on m.symbol = b.symbol and m.cal_date = b.end_date
join {{ ref('pnl_market_daily') }} bm on bm.symbol = b.bench_symbol and bm.cal_date = b.end_date
