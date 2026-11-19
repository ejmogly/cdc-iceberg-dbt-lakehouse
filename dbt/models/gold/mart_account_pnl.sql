-- 계좌별 손익 합(화면 전체 탭의 증권사별 줄과 같은 값, 2026-11-19, 결정 기록 0013 뒤 「전체 투자 보기」).
-- 계좌 x 종목 손익(pnl_position, 기준일은 데이터의 마지막 날)을 계좌로 더합니다. 화면 계좌는 증권사마다 A-SCREEN-<코드>입니다.
-- 평가액은 남은 수량 x 그날 종가 x 그날 원/달러(국내는 1)를 계좌, 종목마다 원 단위로 반올림한 뒤 더합니다
-- (ai/screen_server.py api_portfolio와 같은 규칙). 실현, 평가, 합계, 넣은 원화는 pnl_position 값을 그대로 더합니다.
with pos as (
  select
    element_at(split(p.position_id, ':'), 1) as account_id,
    b.symbol,
    b.end_date,
    p.qty_left,
    p.realized_krw,
    p.unrealized_krw,
    p.total_krw,
    p.invested_krw,
    round(p.qty_left * m.close_filled * case when b.currency = 'USD' then m.usdkrw else 1 end, 0) as value_krw
  from {{ ref('pnl_position') }} p
  join {{ ref('pnl_position_base') }} b on b.position_id = p.position_id
  join {{ ref('pnl_market_daily') }} m on m.symbol = b.symbol and m.cal_date = b.end_date
)

select
  account_id,
  max(end_date) as asof_date,
  count(*) as positions,
  sum(case when qty_left > 0 then 1 else 0 end) as positions_open,
  sum(realized_krw) as realized_krw,
  sum(unrealized_krw) as unrealized_krw,
  sum(total_krw) as total_krw,
  sum(invested_krw) as invested_krw,
  sum(value_krw) as value_krw
from pos
group by account_id
