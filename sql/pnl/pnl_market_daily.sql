-- 종목과 달력의 날마다 쓸 종가와 원/달러 환율. 그날 값이 없으면 직전 값을 씁니다(E01 R03, R04, R09).
-- 달력(pnl_calendar)은 시세와 환율이 있는 날을 모두 담아야 합니다. 실제 데이터에서는 날짜 축 전체입니다.
-- 직전 값 채우기는 IGNORE NULLS 없이, 값이 나올 때마다 늘어나는 묶음 번호로 합니다(엔진마다 지원이 달라서).
with days as (
  select s.symbol, c.cal_date
  from {{ ref('pnl_instruments') }} s
  cross join {{ ref('pnl_calendar') }} c
),

joined as (
  select
    d.symbol,
    d.cal_date,
    p.close_price,
    p.low_price,
    p.high_price,
    count(p.close_price) over (
      partition by d.symbol order by d.cal_date
      rows between unbounded preceding and current row
    ) as close_grp
  from days d
  left join {{ ref('pnl_prices') }} p
    on p.symbol = d.symbol and p.trade_date = d.cal_date
),

fx as (
  select
    c.cal_date,
    f.rate,
    count(f.rate) over (
      order by c.cal_date
      rows between unbounded preceding and current row
    ) as fx_grp
  from {{ ref('pnl_calendar') }} c
  left join {{ ref('pnl_fx') }} f on f.rate_date = c.cal_date
),

fx_filled as (
  select cal_date, max(rate) over (partition by fx_grp) as usdkrw
  from fx
)

select
  j.symbol,
  j.cal_date,
  j.close_price is not null as is_trading_day,
  j.low_price,
  j.high_price,
  max(j.close_price) over (partition by j.symbol, j.close_grp) as close_filled,
  x.usdkrw
from joined j
join fx_filled x on x.cal_date = j.cal_date
