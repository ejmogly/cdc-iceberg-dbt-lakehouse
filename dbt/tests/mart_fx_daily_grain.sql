-- 집계 단위 검사: 같은 고시일이 두 행 이상이면 안 됩니다.
select rate_date, count(*) as n
from {{ ref('mart_fx_daily') }}
group by rate_date
having count(*) > 1
