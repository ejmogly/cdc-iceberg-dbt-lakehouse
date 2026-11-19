-- 매매 한 줄마다 그 줄이 만든 돈의 움직임: 매수 금액, 매도 손익, 효과 나누기, 기준 종목의 같은 매매.
-- 앞 상태(before)는 장부의 바로 앞줄, 첫 줄이면 0입니다.
--   실현 손익(원) = 매도 금액(원) - 덜어 낸 원화 원가(R02).
--   효과 나누기(R10): af = 원화 원가 / 현지 원가, avg = 현지 원가 / 수량.
--     가격 = q x (p - avg) x af, 환율 = q x avg x (fx - af), 교차 = q x (p - avg) x (fx - af). 셋의 합 = 실현 손익.
--   기준 종목(R13): 매수 금액만큼 기준 종목을 사고, 매도 때 같은 비율로 팜.
-- 덜어 낸 원가는 decimal로 두고(전량 매도면 정확), 평균 단가와 평균 환율의 곱이 드는 효과와 기준 종목은 double로 셉니다.
-- decimal(38,18)끼리 곱하면 자리가 넘칩니다(DuckDB 오류). 둘 다 마지막 반올림 전에 소수 여섯째 자리로 맞춥니다.
with s as (
  select
    l.*,
    lag(l.qty_after, 1, 0) over w as qty_before,
    lag(l.cost_local_after, 1, cast(0 as decimal(38, 18))) over w as cost_local_before,
    lag(l.cost_krw_after, 1, cast(0 as decimal(38, 18))) over w as cost_krw_before,
    lag(l.bench_units_after, 1, cast(0 as decimal(38, 18))) over w as bench_units_before
  from {{ ref('pnl_ledger') }} l
  window w as (partition by l.position_id order by l.step)
),

flagged as (
  select
    s.*,
    s.side = 'BUY' as is_buy,
    s.side = 'SELL' and s.qty <= s.qty_before as is_sell,
    s.side = 'SELL' and s.qty > s.qty_before as oversold,
    s.side = 'SELL' and s.qty = s.qty_before as is_full_sell,
    s.qty * s.calc_price * s.calc_fx as amount_krw
  from s
),

sold as (
  select
    f.*,
    case when f.is_sell then
      case when f.is_full_sell then f.cost_local_before
           else cast(f.cost_local_before * f.qty / f.qty_before as decimal(38, 18)) end
    end as out_local,
    case when f.is_sell then
      case when f.is_full_sell then f.cost_krw_before
           else cast(f.cost_krw_before * f.qty / f.qty_before as decimal(38, 18)) end
    end as out_krw,
    case when f.is_sell then cast(f.cost_local_before as double) / f.qty_before end as avg_before,
    case when f.is_sell then cast(f.cost_krw_before as double) / cast(f.cost_local_before as double) end as af_before,
    -- 기준 종목 단위는 double로 나눕니다. Spark는 decimal(38,18) x 수량 / 수량에서 정밀도가 넘쳐 소수 여섯째 자리로 줄이는데,
    -- 단위에 기준 종목의 원화 값(약 100만 원)이 곱해져 그 오차가 0.5원까지 커집니다. 2026-11-19 화면 매매에서 기준선이
    -- DuckDB와 1원 달랐습니다(장부 pnl_ledger의 Spark 판과 같은 처리, sql/pnl/README.md 엔진 함정 3, 4).
    case when f.is_sell then
      case when f.is_full_sell then f.bench_units_before
           else cast(cast(f.bench_units_before as double) * f.qty / f.qty_before as decimal(38, 18)) end
    end as bench_units_out
  from flagged f
)

select
  position_id,
  step,
  trade_seq,
  calc_date,
  side,
  qty,
  calc_price,
  calc_fx,
  is_buy,
  is_sell,
  oversold,
  qty_before,
  qty_after,
  cost_krw_after,
  case when is_buy then amount_krw else 0 end as invested_krw,
  case when is_sell then amount_krw else 0 end as proceeds_krw,
  case when is_buy then -amount_krw when is_sell then amount_krw else 0 end as cash_flow_krw,
  case when is_buy then -cast(amount_krw as double)
       when is_sell then cast(bench_units_out as double) * cast(bench_px_krw as double) else 0 end as bench_flow_krw,
  case when is_sell then qty * calc_price - out_local else 0 end as realized_local,
  case when is_sell then amount_krw - out_krw else 0 end as realized_krw,
  case when is_sell then qty * (calc_price - avg_before) * af_before else 0 end as price_effect_krw,
  case when is_sell then qty * avg_before * (calc_fx - af_before) else 0 end as fx_effect_krw,
  case when is_sell then qty * (calc_price - avg_before) * (calc_fx - af_before) else 0 end as cross_effect_krw,
  avg_before
from sold
