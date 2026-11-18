-- 값 범위 검사: 가격은 0보다 크고, 고가는 저가 이상이며, 시가와 종가는 그날 고가와 저가 사이, 거래량은 0 이상.
-- 원본을 잘못 읽었거나(칸 바뀜) 적재 중 값이 바뀌면 걸립니다.
select symbol, trade_date, open_price, high_price, low_price, close_price, volume
from {{ ref('mart_symbol_daily') }}
where open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0
   or high_price < low_price
   or open_price > high_price or open_price < low_price
   or close_price > high_price or close_price < low_price
   or volume < 0
