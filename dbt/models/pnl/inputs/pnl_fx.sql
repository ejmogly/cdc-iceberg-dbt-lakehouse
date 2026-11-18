-- 손익 모델의 입력 pnl_fx(고시일 = 1행). Gold 환율 마트의 원/달러 매매기준율입니다.
select rate_date, usdkrw_rate as rate
from {{ ref('mart_fx_daily') }}
