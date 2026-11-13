-- 집계 단위 검사: 같은 종목, 같은 거래일이 두 행 이상이면 안 됩니다.
select symbol, trade_date, count(*) as n
from {{ ref('mart_daily_symbol_trading') }}
group by symbol, trade_date
having count(*) > 1
