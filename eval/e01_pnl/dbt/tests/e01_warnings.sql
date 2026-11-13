-- E01 경고: 코드와 줄 번호가 정답과 같은지. 섞은 사본의 줄 번호는 in_seq_map으로 원래 번호로 되돌립니다.
{{ config(tags=['golden']) }}
with got as (
  select w.position_id, w.code, s.orig_seq as trade_seq
  from {{ ref('pnl_warnings') }} w
  join {{ source('e01', 'in_seq_map') }} s using (position_id, trade_seq)
),
want as (
  select m.position_id, e.code, e.trade_seq
  from {{ source('e01', 'in_case_map') }} m
  join {{ source('e01', 'exp_warnings') }} e on e.case_id = m.case_id
)

(select 'SQL에만' as side, * from got except all select 'SQL에만', * from want)
union all
(select '정답에만' as side, * from want except all select '정답에만', * from got)
