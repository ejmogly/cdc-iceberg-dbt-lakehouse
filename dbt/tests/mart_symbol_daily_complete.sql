-- 빠짐 검사: Silver의 지워지지 않은 시세 행이 모두 마트에 있어야 합니다.
-- 종목 정보(seeds/instruments.csv)에 없는 종목은 inner join에서 조용히 빠지므로 행 수로 잡습니다.
select s.n as silver_rows, m.n as mart_rows
from (select count(*) as n from {{ source('silver', 'daily_prices') }} where not is_deleted) as s
cross join (select count(*) as n from {{ ref('mart_symbol_daily') }}) as m
where s.n <> m.n
