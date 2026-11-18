-- 손익 모델의 입력 pnl_instruments(종목 = 1행).
select symbol, currency
from {{ ref('instruments') }}
