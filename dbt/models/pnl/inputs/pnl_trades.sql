-- 손익 모델의 입력 pnl_trades(체결 = 1행). Gold fct_execution의 체결가와 환율을 그대로 넣습니다(국내 종목은 환율 NULL).
-- 같은 계산 단위 안의 줄 번호는 (체결 시각, 체결 번호) 순서입니다(R05, sql/pnl/README.md). 화면 매매는 모두 그날 장 마감
-- 시각이라 같은 날 안에서는 체결 번호(줄 순서대로 매겨짐, loaders/screen_trades.py)로 가립니다. 장부는 날짜를 먼저 봅니다.
select
  concat(account_id, ':', symbol) as position_id,
  cast(row_number() over (partition by account_id, symbol order by executed_at, execution_id) - 1 as int) as trade_seq,
  trade_date,
  side,
  exec_qty as qty,
  exec_price as price,
  fx_rate
from {{ ref('fct_execution') }}
