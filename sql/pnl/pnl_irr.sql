-- 연 환산 수익률(R12): 원화 현금 흐름(매수 -, 매도 +, 기준일의 남은 평가액 +)의 내부수익률. 기준 종목도 같은 방식(R13).
-- 날짜 차 / 365로 햇수를 셉니다. 첫 매매일부터 기준일까지 30일 미만이거나, 범위 -99% ~ +100,000%의
-- 양 끝에서 현재가치의 부호가 같으면 없음(null)입니다.
-- 반복 계산(이분법) 대신 x = ln(1 + r)의 격자를 두 번 좁히고(101점씩) 마지막 칸에서 직선으로 맞춥니다.
-- 재귀 CTE 안에서 집계를 못 하는 엔진이 많아서입니다. 부호가 바뀌는 칸이 여럿이면 가장 낮은 쪽을 고릅니다.
with flows as (
  select position_id, 'own' as flow_kind, calc_date as flow_date, cash_flow_krw as amount
  from {{ ref('pnl_trade_result') }}
  where is_buy or is_sell

  union all

  select position_id, 'bench', calc_date, bench_flow_krw
  from {{ ref('pnl_trade_result') }}
  where is_buy or is_sell

  union all

  select position_id, 'own', end_date, case when qty_end = 0 then 0 else qty_end * px_end * fx_end end
  from {{ ref('pnl_position_end') }}

  union all

  select position_id, 'bench', end_date, bench_units_end * bench_px_krw_end
  from {{ ref('pnl_position_end') }}
),

eligible as (
  select r.position_id, min(r.calc_date) as first_date
  from {{ ref('pnl_trade_result') }} r
  where r.is_buy or r.is_sell
  group by r.position_id
  having sum(r.invested_krw) > 0
),

timed as (
  select
    f.position_id,
    f.flow_kind,
    cast(f.amount as double) as v,
    {{ dbt.datediff("e.first_date", "f.flow_date", "day") }} / 365.0 as t
  from flows f
  join eligible e on e.position_id = f.position_id
  join {{ ref('pnl_position_end') }} p on p.position_id = f.position_id
  where {{ dbt.datediff("e.first_date", "p.end_date", "day") }} >= 30
),

digits as (
  select d from (values (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)) as v (d)
),

grid as (
  select a.d * 10 + b.d as j from digits a cross join digits b
  union all
  select 100
),

range0 as (
  select distinct position_id, flow_kind, ln(0.01) as lo, ln(1001.0) as hi
  from timed
),

npv1 as (
  select
    r.position_id,
    r.flow_kind,
    g.j,
    r.lo + (r.hi - r.lo) * g.j / 100.0 as x,
    sum(t.v * exp(-(r.lo + (r.hi - r.lo) * g.j / 100.0) * t.t)) as npv
  from range0 r
  cross join grid g
  join timed t on t.position_id = r.position_id and t.flow_kind = r.flow_kind
  group by r.position_id, r.flow_kind, g.j, r.lo, r.hi
),

has_root as (
  select position_id, flow_kind
  from npv1
  group by position_id, flow_kind
  having max(case when j = 0 then npv end) * max(case when j = 100 then npv end) <= 0
),

cells1 as (
  select
    n.position_id,
    n.flow_kind,
    n.j,
    n.x,
    lead(n.x) over w as x_next,
    n.npv,
    lead(n.npv) over w as npv_next
  from npv1 n
  join has_root h on h.position_id = n.position_id and h.flow_kind = n.flow_kind
  window w as (partition by n.position_id, n.flow_kind order by n.j)
),

range1 as (
  select position_id, flow_kind, x as lo, x_next as hi
  from (
    select c.*, row_number() over (partition by c.position_id, c.flow_kind order by c.j) as rn
    from cells1 c
    where c.npv * c.npv_next <= 0
  ) z
  where rn = 1
),

npv2 as (
  select
    r.position_id,
    r.flow_kind,
    g.j,
    r.lo + (r.hi - r.lo) * g.j / 100.0 as x,
    sum(t.v * exp(-(r.lo + (r.hi - r.lo) * g.j / 100.0) * t.t)) as npv
  from range1 r
  cross join grid g
  join timed t on t.position_id = r.position_id and t.flow_kind = r.flow_kind
  group by r.position_id, r.flow_kind, g.j, r.lo, r.hi
),

cells2 as (
  select
    n.position_id,
    n.flow_kind,
    n.j,
    n.x,
    lead(n.x) over w as x_next,
    n.npv,
    lead(n.npv) over w as npv_next
  from npv2 n
  window w as (partition by n.position_id, n.flow_kind order by n.j)
),

root as (
  select
    position_id,
    flow_kind,
    case when npv_next = npv then x
         else x - npv * (x_next - x) / (npv_next - npv) end as x_root
  from (
    select c.*, row_number() over (partition by c.position_id, c.flow_kind order by c.j) as rn
    from cells2 c
    where c.npv * c.npv_next <= 0
  ) z
  where rn = 1
)

select
  b.position_id,
  max(case when r.flow_kind = 'own'
           then round(cast((exp(r.x_root) - 1) * 100 as decimal(38, 6)), 2) end) as irr_pct,
  max(case when r.flow_kind = 'bench'
           then round(cast((exp(r.x_root) - 1) * 100 as decimal(38, 6)), 2) end) as bench_irr_pct
from {{ ref('pnl_position_base') }} b
left join root r on r.position_id = b.position_id
group by b.position_id
