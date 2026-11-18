-- 손익 모델의 입력 pnl_calendar(달력 날 = 1행). 화면 데이터 파일과 같이 시세가 있는 날과 환율이 있는 날을 합칩니다.
select trade_date as cal_date from {{ ref('mart_symbol_daily') }}
union
select rate_date from {{ ref('mart_fx_daily') }}
