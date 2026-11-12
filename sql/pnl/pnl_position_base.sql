-- 계산 단위(계좌 x 종목, 평가셋에서는 사례 하나)마다 통화, 기준 종목, 기준일.
-- end_date는 기준일을 달력에 맞춘 날(기준일 이하 마지막 날)입니다.
select
  p.position_id,
  p.symbol,
  p.bench_symbol,
  i.currency,
  p.asof_date,
  max(c.cal_date) as end_date
from {{ ref('pnl_positions') }} p
join {{ ref('pnl_instruments') }} i on i.symbol = p.symbol
join {{ ref('pnl_calendar') }} c on c.cal_date <= p.asof_date
group by p.position_id, p.symbol, p.bench_symbol, i.currency, p.asof_date
