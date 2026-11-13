-- E01 매도 한 건씩: 날짜, 수량, 체결가, 직전 평균 단가, 실현 손익(원)이 정답과 순서까지 같은지.
-- 양쪽에서 상대에게 없는 행을 돌려줍니다(except all이라 같은 행이 두 번이면 두 번 셉니다).
{{ config(tags=['golden']) }}
with got as (
  select position_id, row_number() over (partition by position_id order by step) as n,
         sell_date, qty, price, avg_cost, pnl_krw
  from {{ ref('pnl_sells') }}
),
want as (
  select m.position_id, e.n, e.sell_date, e.qty, e.price, e.avg_cost, e.pnl_krw
  from {{ source('e01', 'in_case_map') }} m
  join {{ source('e01', 'exp_sells') }} e on e.case_id = m.case_id
)

(select 'SQL에만' as side, * from got except all select 'SQL에만', * from want)
union all
(select '정답에만' as side, * from want except all select '정답에만', * from got)
