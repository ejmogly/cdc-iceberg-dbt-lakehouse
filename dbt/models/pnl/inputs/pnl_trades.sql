-- 손익 모델의 입력 pnl_trades(체결 = 1행). Gold fct_execution의 체결가와 환율을 그대로 넣습니다(국내 종목은 환율 NULL).
-- 같은 계산 단위 안의 줄 번호는 체결 번호 순서입니다(화면 매매는 줄 순서대로 번호가 매겨짐, loaders/screen_trades.py).
select
  concat(account_id, ':', symbol) as position_id,
  cast(row_number() over (partition by account_id, symbol order by execution_id) - 1 as int) as trade_seq,
  trade_date,
  side,
  exec_qty as qty,
  exec_price as price,
  fx_rate
from {{ ref('fct_execution') }}
