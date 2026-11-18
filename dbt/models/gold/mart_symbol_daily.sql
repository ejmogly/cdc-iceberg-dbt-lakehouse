-- 종목 x 거래일(그 시장의 현지 날짜) = 1행. 내 투자 돌아보기 화면(docs/ai/F-screen.md)이 읽는 시세 마트입니다.
-- Silver 현재 상태(sql/silver의 MERGE)에서 원천에서 지워진 행을 빼고, 종목 정보(시장, 통화, 이름)와
-- 미국 종목이면 그날의 원/달러 매매기준율(한국 휴일이라 없으면 NULL)을 붙입니다.
-- 가격은 수정하지 않은 값 그대로입니다(수정 종가는 원천에 없음, 설계서 6절 2번).
-- 종목 정보는 1행씩이고 환율도 (통화쌍, 날짜)마다 1행이라 조인이 행을 늘리지 않습니다(tests/mart_symbol_daily_grain.sql).
select
  p.symbol,
  i.name,
  i.market,
  i.currency,
  p.trade_date,
  p.open_price,
  p.high_price,
  p.low_price,
  p.close_price,
  p.volume,
  case when i.currency = 'USD' then fx.rate end as usdkrw_rate,
  p.source_lsn,
  p.source_ts
from {{ source('silver', 'daily_prices') }} as p
inner join {{ ref('instruments') }} as i
  on i.symbol = p.symbol
left join {{ source('silver', 'fx_rates') }} as fx
  on fx.currency_pair = 'USDKRW'
  and fx.rate_date = p.trade_date
  and not fx.is_deleted
where not p.is_deleted
