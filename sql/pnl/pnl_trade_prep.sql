-- 매매 한 줄마다 계산에 쓸 날짜, 가격, 환율과 입력 단계의 경고를 붙이고 처리 순서(step)를 매깁니다.
--   calc_date: 매매일 이하 마지막 달력 날(R06). 달력에 없는 날이면 직전 날로 맞춥니다.
--   calc_price: 체결가, 비었으면 그날 종가(직전 값 포함, R03).
--   calc_fx: 미국 종목은 체결 환율, 비었으면 그날 기준율(직전 값 포함, R03, R04). 국내 종목은 1.
--   bench_px_krw: 기준 종목 1단위의 그날 원화 값(R13).
--   step: 수량이 0 이하(BAD_QTY)이거나 기준일 뒤(AFTER_ASOF)인 줄을 뺀 나머지에 날짜, 입력 순서로 1, 2, ...(R05, R07).
--         보유보다 많이 판 줄(OVERSOLD)은 장부(pnl_ledger)에서 가립니다.
with snapped as (
  select
    t.position_id,
    t.trade_seq,
    t.trade_date,
    t.side,
    t.qty,
    t.price,
    t.fx_rate,
    max(c.cal_date) as calc_date
  from {{ ref('pnl_trades') }} t
  join {{ ref('pnl_calendar') }} c on c.cal_date <= t.trade_date
  group by t.position_id, t.trade_seq, t.trade_date, t.side, t.qty, t.price, t.fx_rate
),

checked as (
  select
    s.*,
    b.symbol,
    b.bench_symbol,
    b.currency,
    b.end_date,
    s.qty <= 0 as bad_qty,
    s.qty > 0 and s.calc_date > b.end_date as after_asof
  from snapped s
  join {{ ref('pnl_position_base') }} b on b.position_id = s.position_id
),

priced as (
  select
    k.*,
    coalesce(k.price, m.close_filled) as calc_price,
    case when k.currency = 'USD' then coalesce(k.fx_rate, m.usdkrw) else 1 end as calc_fx,
    bm.close_filled * bm.usdkrw as bench_px_krw,
    own.close_price is null as non_trading_day,
    k.price is not null and own.close_price is not null
      and (k.price < own.low_price or k.price > own.high_price) as price_out_of_range
  from checked k
  join {{ ref('pnl_market_daily') }} m
    on m.symbol = k.symbol and m.cal_date = k.calc_date
  join {{ ref('pnl_market_daily') }} bm
    on bm.symbol = k.bench_symbol and bm.cal_date = k.calc_date
  left join {{ ref('pnl_prices') }} own
    on own.symbol = k.symbol and own.trade_date = k.trade_date
)

select
  p.*,
  case when not (p.bad_qty or p.after_asof) then
    row_number() over (
      partition by p.position_id, (p.bad_qty or p.after_asof)
      order by p.calc_date, p.trade_seq
    )
  end as step
from priced p
