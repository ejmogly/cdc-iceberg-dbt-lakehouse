-- E01 숫자 일치: 정답 칸 16개는 정확히, 연 환산 둘은 0.05%p 안(E01 3절). 섞은 사본은 원래 사례의 정답과 맞춥니다.
-- 돌려주는 행이 어긋난 계산 단위입니다. 정답이 없거나 결과가 없는 계산 단위도 돌려줍니다.
{{ config(tags=['golden']) }}
{% set exact = ['qty_left', 'avg_cost_local', 'avg_buy_fx', 'realized_local', 'unrealized_local', 'total_local',
                'realized_krw', 'unrealized_krw', 'total_krw', 'invested_krw', 'simple_return_pct', 'price_effect_krw',
                'fx_effect_krw', 'cross_effect_krw', 'bench_total_krw', 'max_drawdown_krw'] %}
with want as (
  select m.position_id, e.*
  from {{ source('e01', 'in_case_map') }} m
  join {{ source('e01', 'exp_position') }} e on e.case_id = m.case_id
)

select coalesce(g.position_id, w.position_id) as position_id
from {{ ref('pnl_position') }} g
full outer join want w on g.position_id = w.position_id
where g.position_id is null
   or w.position_id is null
{%- for c in exact %}
   or g.{{ c }} is distinct from w.{{ c }}
{%- endfor %}
{%- for c in ['irr_pct', 'bench_irr_pct'] %}
   or (g.{{ c }} is null) <> (w.{{ c }} is null)
   or abs(g.{{ c }} - w.{{ c }}) > 0.05
{%- endfor %}
