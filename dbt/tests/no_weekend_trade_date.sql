-- 장이 열리지 않는 토요일(7)과 일요일(1)의 거래일이 보이면 시간대 규칙이 틀린 것입니다.
-- 미국 종목의 금요일 밤 체결을 서울 날짜로 바꾸면 토요일이 됩니다(가이드 05 12절 ⑤).
select execution_id, symbol, market, executed_at, trade_date
from {{ ref('fct_execution') }}
where dayofweek(trade_date) in (1, 7)
