-- 계산 단위(계좌 x 종목)마다 기준일의 손익 요약. 칸 이름은 E01 정답(expected.json)과 같습니다.
-- 반올림(R15): 계산 중에는 하지 않고 여기서 한 번. 원은 정수, 달러는 둘째, 단가와 환율은 넷째, 수익률은 둘째 자리.
--   0.5는 0에서 먼 쪽으로(decimal의 round). 엔진에 따라 나눗셈이 double로 돌아 1e-10 수준의 오차가 생기므로
--   먼저 소수 여섯째 자리로 맞춘 뒤 반올림합니다. 정확한 값이 .5에서 0.0000005 안쪽일 때만 결과가 달라질 수 있습니다.
-- 항등식 검사용 gap_* 칸은 반올림 전 값입니다. 합계를 현금 흐름(매도 금액 + 남은 평가액 - 매수 금액)으로 따로 내어
-- 실현 + 평가, 가격 + 환율 + 교차와 비교합니다.
with agg as (
  select
    position_id,
    sum(invested_krw) as invested_krw,
    sum(proceeds_krw) as proceeds_krw,
    sum(realized_local) as realized_local,
    sum(realized_krw) as realized_krw,
    sum(price_effect_krw) as price_effect_krw,
    sum(fx_effect_krw) as fx_effect_krw,
    sum(cross_effect_krw) as cross_effect_krw,
    sum(case when is_sell then bench_flow_krw else 0 end) as bench_cash_krw
  from {{ ref('pnl_trade_result') }}
  group by position_id
),

drawdown as (
  select position_id, least(0, min(pnl_cum_krw - peak_krw)) as max_drawdown_krw
  from {{ ref('pnl_daily') }}
  group by position_id
),

base as (
  select
    e.position_id,
    e.currency,
    e.qty_end,
    e.cost_local_end,
    e.cost_krw_end,
    e.px_end,
    e.fx_end,
    coalesce(a.invested_krw, 0) as invested_krw,
    coalesce(a.proceeds_krw, 0) as proceeds_krw,
    coalesce(a.realized_local, 0) as realized_local,
    coalesce(a.realized_krw, 0) as realized_krw,
    coalesce(a.bench_cash_krw, 0) + e.bench_units_end * e.bench_px_krw_end - coalesce(a.invested_krw, 0)
      as bench_total_krw,
    case when e.qty_end > 0 then e.qty_end * e.px_end - e.cost_local_end else 0 end as unrealized_local,
    case when e.qty_end > 0 then e.qty_end * e.px_end * e.fx_end - e.cost_krw_end else 0 end as unrealized_krw,
    case when e.qty_end > 0 then e.qty_end * e.px_end * e.fx_end else 0 end as end_value_krw,
    case when e.qty_end > 0 then e.cost_local_end / e.qty_end end as avg_end,
    case when e.qty_end > 0 then e.cost_krw_end / e.cost_local_end end as af_end,
    coalesce(a.price_effect_krw, 0) as price_sell,
    coalesce(a.fx_effect_krw, 0) as fx_sell,
    coalesce(a.cross_effect_krw, 0) as cross_sell,
    coalesce(d.max_drawdown_krw, 0) as max_drawdown_krw
  from {{ ref('pnl_position_end') }} e
  left join agg a on a.position_id = e.position_id
  left join drawdown d on d.position_id = e.position_id
),

totals as (
  select
    b.*,
    b.realized_local + b.unrealized_local as total_local,
    b.realized_krw + b.unrealized_krw as total_krw,
    b.proceeds_krw + b.end_value_krw - b.invested_krw as total_krw_cash,
    b.price_sell + case when b.qty_end > 0 then b.qty_end * (b.px_end - b.avg_end) * b.af_end else 0 end
      as price_effect_krw,
    b.fx_sell + case when b.qty_end > 0 then b.qty_end * b.avg_end * (b.fx_end - b.af_end) else 0 end
      as fx_effect_krw,
    b.cross_sell + case when b.qty_end > 0 then b.qty_end * (b.px_end - b.avg_end) * (b.fx_end - b.af_end) else 0 end
      as cross_effect_krw
  from base b
)

select
  t.position_id,
  t.qty_end as qty_left,
  case when t.qty_end > 0 then round(round(cast(t.avg_end as decimal(38, 12)), 6), 4) end as avg_cost_local,
  case when t.qty_end > 0 and t.currency = 'USD' then round(round(cast(t.af_end as decimal(38, 12)), 6), 4) end
    as avg_buy_fx,
  -- 현지 통화 칸은 달러면 둘째 자리, 원이면 정수. 두 갈래의 자료형을 같게 맞춥니다
  -- (DuckDB는 decimal(38,2)와 decimal(38,0)을 섞으면 decimal(38,0)으로 합쳐 소수를 버립니다).
  case when t.currency = 'USD' then cast(round(round(cast(t.realized_local as decimal(38, 12)), 6), 2) as decimal(38, 2))
       else cast(round(round(cast(t.realized_local as decimal(38, 12)), 6), 0) as decimal(38, 2)) end as realized_local,
  case when t.currency = 'USD' then cast(round(round(cast(t.unrealized_local as decimal(38, 12)), 6), 2) as decimal(38, 2))
       else cast(round(round(cast(t.unrealized_local as decimal(38, 12)), 6), 0) as decimal(38, 2)) end as unrealized_local,
  case when t.currency = 'USD' then cast(round(round(cast(t.total_local as decimal(38, 12)), 6), 2) as decimal(38, 2))
       else cast(round(round(cast(t.total_local as decimal(38, 12)), 6), 0) as decimal(38, 2)) end as total_local,
  round(round(cast(t.realized_krw as decimal(38, 12)), 6), 0) as realized_krw,
  round(round(cast(t.unrealized_krw as decimal(38, 12)), 6), 0) as unrealized_krw,
  round(round(cast(t.total_krw as decimal(38, 12)), 6), 0) as total_krw,
  round(round(cast(t.invested_krw as decimal(38, 12)), 6), 0) as invested_krw,
  case when t.invested_krw > 0
       then round(round(cast(t.total_krw / t.invested_krw * 100 as decimal(38, 12)), 6), 2) end as simple_return_pct,
  round(round(cast(t.price_effect_krw as decimal(38, 12)), 6), 0) as price_effect_krw,
  round(round(cast(t.fx_effect_krw as decimal(38, 12)), 6), 0) as fx_effect_krw,
  round(round(cast(t.cross_effect_krw as decimal(38, 12)), 6), 0) as cross_effect_krw,
  round(round(cast(t.bench_total_krw as decimal(38, 12)), 6), 0) as bench_total_krw,
  round(round(cast(t.max_drawdown_krw as decimal(38, 12)), 6), 0) as max_drawdown_krw,
  i.irr_pct,
  i.bench_irr_pct,
  round(round(cast(t.total_local * t.fx_end as decimal(38, 12)), 6), 0) as naive_krw,
  cast(t.realized_krw + t.unrealized_krw - t.total_krw_cash as double) as gap_realized_unrealized,
  cast(t.price_effect_krw + t.fx_effect_krw + t.cross_effect_krw - t.total_krw_cash as double) as gap_effects
from totals t
left join {{ ref('pnl_irr') }} i on i.position_id = t.position_id
