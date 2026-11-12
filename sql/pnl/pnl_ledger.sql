-- 이동평균법 장부. 매매 한 줄(step)마다 그 뒤의 보유 수량, 보유 원가(현지 통화, 원), 기준 종목 보유 단위.
-- 앞줄 결과에 기대므로 창 함수 하나로는 안 되고 재귀 CTE로 한 줄씩 넘깁니다(E01 「미리 예상하는 실패」 첫 줄).
--   매수: 수량과 원가를 더함. 원화 원가는 체결가 x 그 매매의 환율로 따로 쌓음(R01, R02).
--   매도: 평균 단가를 바꾸지 않고 원가를 같은 비율로 덜어 냄. 전량이면 정확히 0으로 초기화(R01).
--   보유보다 많이 판 줄은 아무것도 바꾸지 않고 넘어감(R07, OVERSOLD는 pnl_trade_result에서 표시).
-- 평균 단가 대신 보유 원가를 들고 다녀서 끝나지 않는 평균 단가(1/3)의 오차가 쌓이지 않게 합니다.
-- 나눗셈은 갈래 안에서 decimal(38,18)로 바꿉니다. CASE에 double 갈래가 섞이면 엔진이 매수 갈래까지 double로 바꿔
-- 정확해야 할 원가에 오차가 붙습니다(DuckDB는 decimal 나눗셈이 double이고, double을 decimal로 바꿀 때도 오차가 생김).
-- 그래서 오차는 일부 매도의 나눗셈에서만 생기고, 마지막 반올림 전에 소수 여섯째 자리로 한 번 맞춥니다(pnl_position).
with recursive ledger (position_id, step, qty_after, cost_local_after, cost_krw_after, bench_units_after) as (
  select
    position_id,
    cast(0 as bigint),
    0,
    cast(0 as decimal(38, 18)),
    cast(0 as decimal(38, 18)),
    cast(0 as decimal(38, 18))
  from {{ ref('pnl_position_base') }}

  union all

  select
    l.position_id,
    cast(t.step as bigint),
    case
      when t.side = 'BUY' then l.qty_after + t.qty
      when t.qty <= l.qty_after then l.qty_after - t.qty
      else l.qty_after
    end,
    cast(case
      when t.side = 'BUY' then l.cost_local_after + t.qty * t.calc_price
      when t.qty = l.qty_after then 0
      when t.qty < l.qty_after then l.cost_local_after - cast(l.cost_local_after * t.qty / l.qty_after as decimal(38, 18))
      else l.cost_local_after
    end as decimal(38, 18)),
    cast(case
      when t.side = 'BUY' then l.cost_krw_after + t.qty * t.calc_price * t.calc_fx
      when t.qty = l.qty_after then 0
      when t.qty < l.qty_after then l.cost_krw_after - cast(l.cost_krw_after * t.qty / l.qty_after as decimal(38, 18))
      else l.cost_krw_after
    end as decimal(38, 18)),
    cast(case
      when t.side = 'BUY' then l.bench_units_after + cast(t.qty * t.calc_price * t.calc_fx / t.bench_px_krw as decimal(38, 18))
      when t.qty = l.qty_after then 0
      when t.qty < l.qty_after then l.bench_units_after - cast(l.bench_units_after * t.qty / l.qty_after as decimal(38, 18))
      else l.bench_units_after
    end as decimal(38, 18))
  from ledger l
  join {{ ref('pnl_trade_prep') }} t
    on t.position_id = l.position_id and t.step = l.step + 1
)

select
  l.position_id,
  l.step,
  t.trade_seq,
  t.calc_date,
  t.side,
  t.qty,
  t.calc_price,
  t.calc_fx,
  t.bench_px_krw,
  l.qty_after,
  l.cost_local_after,
  l.cost_krw_after,
  l.bench_units_after
from ledger l
join {{ ref('pnl_trade_prep') }} t
  on t.position_id = l.position_id and t.step = l.step
where l.step > 0
