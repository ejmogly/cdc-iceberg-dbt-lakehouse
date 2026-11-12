-- 계산에서 뺐거나 의심스러운 매매 줄(R06, R07, R08). trade_seq는 입력 순서 번호입니다.
--   BAD_QTY: 수량 0 이하라 뺌. AFTER_ASOF: 기준일 뒤라 뺌. OVERSOLD: 보유보다 많이 팔아 뺌.
--   NON_TRADING_DAY: 그 종목의 시세가 없는 날의 매매(직전 날 값으로 계산). PRICE_OUT_OF_RANGE: 체결가가 그날 저가와 고가 밖.
select position_id, 'BAD_QTY' as code, trade_seq
from {{ ref('pnl_trade_prep') }}
where bad_qty

union all

select position_id, 'AFTER_ASOF', trade_seq
from {{ ref('pnl_trade_prep') }}
where after_asof

union all

select position_id, 'NON_TRADING_DAY', trade_seq
from {{ ref('pnl_trade_prep') }}
where step is not null and non_trading_day

union all

select position_id, 'PRICE_OUT_OF_RANGE', trade_seq
from {{ ref('pnl_trade_prep') }}
where step is not null and price_out_of_range

union all

select position_id, 'OVERSOLD', trade_seq
from {{ ref('pnl_trade_result') }}
where oversold
