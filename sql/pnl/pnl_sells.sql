-- 매도 한 건마다 화면에 보일 값: 날짜, 수량, 체결가, 매도 직전 평균 단가, 그 매도의 실현 손익(원).
-- 반올림은 pnl_position과 같습니다(R15).
select
  position_id,
  step,
  trade_seq,
  calc_date as sell_date,
  qty,
  round(round(cast(calc_price as decimal(38, 12)), 6), 4) as price,
  round(round(cast(avg_before as decimal(38, 12)), 6), 4) as avg_cost,
  round(round(cast(realized_krw as decimal(38, 12)), 6), 0) as pnl_krw
from {{ ref('pnl_trade_result') }}
where is_sell
