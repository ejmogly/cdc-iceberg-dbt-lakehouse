-- 고시일 = 1행. 원/달러 매매기준율(한국은행 ECOS)입니다. 화면은 매매일의 환율과 기준일 평가 환율을 여기서 읽습니다.
-- 한국 휴일에는 행이 없습니다. 그날 미국 매매의 환율은 손익 계산이 직전 값을 쓰고 FX_CARRIED 경고를 냅니다.
select
  rate_date,
  rate as usdkrw_rate,
  source_lsn,
  source_ts
from {{ source('silver', 'fx_rates') }}
where currency_pair = 'USDKRW'
  and not is_deleted
