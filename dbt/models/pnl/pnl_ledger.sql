-- 이동평균법 장부(Spark 판). 매매 한 줄(step)마다 그 뒤의 보유 수량, 보유 원가(현지 통화, 원), 기준 종목 보유 단위.
-- 규칙은 sql/pnl/pnl_ledger.sql(DuckDB, 재귀 CTE)과 같습니다. Spark 3.5에는 재귀 CTE가 없어(sql/pnl/README.md 엔진 함정 4)
-- 계산 단위마다 매매를 step 순서의 배열로 모은 뒤 aggregate()로 한 줄씩 접어 넘깁니다. 접을 때마다 그 줄 뒤의 상태를
-- out 배열에 붙이고, 끝에 펼쳐 한 줄씩 돌려줍니다.
--   매수: 수량과 원가를 더함. 원화 원가는 체결가 x 그 매매의 환율로 따로 쌓음(R01, R02).
--   매도: 평균 단가를 바꾸지 않고 원가를 같은 비율로 덜어 냄. 전량이면 정확히 0으로 초기화(R01).
--   보유보다 많이 판 줄은 아무것도 바꾸지 않고 넘어감(R07, OVERSOLD는 pnl_trade_result에서 표시).
-- 나눗셈은 DuckDB 판과 같은 자리에서 double로 나눈 뒤 decimal(38,18)로 바꿉니다. DuckDB는 decimal 나눗셈이 double이고,
-- Spark는 decimal 나눗셈의 소수 자리를 여섯째까지 줄여(정밀도 38을 넘을 때) 기준 종목 보유 단위가 어긋났습니다
-- (골든 G28 기준선 손익 1원, 11/18 첫 실행). 다른 파일은 sql/pnl/과 같은 내용입니다
-- (eval/e01_pnl/check_spark.py가 같은지 보고 골든 30개를 Spark에서 맞춥니다).
{%- set q_new -%}
case when t.side = 'BUY' then acc.q + t.qty when t.qty <= acc.q then acc.q - t.qty else acc.q end
{%- endset -%}
{%- set cl_new -%}
cast(case
  when t.side = 'BUY' then acc.cl + t.qty * t.calc_price
  when t.qty = acc.q then 0
  when t.qty < acc.q then acc.cl - cast(cast(acc.cl as double) * t.qty / acc.q as decimal(38, 18))
  else acc.cl
end as decimal(38, 18))
{%- endset -%}
{%- set ck_new -%}
cast(case
  when t.side = 'BUY' then acc.ck + t.qty * t.calc_price * t.calc_fx
  when t.qty = acc.q then 0
  when t.qty < acc.q then acc.ck - cast(cast(acc.ck as double) * t.qty / acc.q as decimal(38, 18))
  else acc.ck
end as decimal(38, 18))
{%- endset -%}
{%- set bu_new -%}
cast(case
  when t.side = 'BUY' then acc.bu + cast(cast(t.qty * t.calc_price * t.calc_fx as double) / cast(t.bench_px_krw as double) as decimal(38, 18))
  when t.qty = acc.q then 0
  when t.qty < acc.q then acc.bu - cast(cast(acc.bu as double) * t.qty / acc.q as decimal(38, 18))
  else acc.bu
end as decimal(38, 18))
{%- endset %}
with prep as (
  select position_id, cast(step as bigint) as step, side, qty, calc_price, calc_fx, bench_px_krw
  from {{ ref('pnl_trade_prep') }}
  where step is not null
),

grouped as (
  -- 구조체 배열은 첫 칸(step) 순서로 정렬됩니다
  select position_id, array_sort(collect_list(struct(step, side, qty, calc_price, calc_fx, bench_px_krw))) as trades
  from prep
  group by position_id
),

folded as (
  select
    position_id,
    aggregate(
      trades,
      named_struct(
        'q', 0,
        'cl', cast(0 as decimal(38, 18)),
        'ck', cast(0 as decimal(38, 18)),
        'bu', cast(0 as decimal(38, 18)),
        'out', cast(array() as array<struct<step: bigint, q: int, cl: decimal(38, 18), ck: decimal(38, 18), bu: decimal(38, 18)>>)
      ),
      (acc, t) -> named_struct(
        'q', {{ q_new }},
        'cl', {{ cl_new }},
        'ck', {{ ck_new }},
        'bu', {{ bu_new }},
        'out', concat(acc.out, array(named_struct(
          'step', t.step, 'q', {{ q_new }}, 'cl', {{ cl_new }}, 'ck', {{ ck_new }}, 'bu', {{ bu_new }})))
      )
    ).out as steps
  from grouped
),

ledger as (
  select f.position_id, s.step, s.q as qty_after, s.cl as cost_local_after, s.ck as cost_krw_after, s.bu as bench_units_after
  from folded f
  lateral view explode(f.steps) e as s
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
