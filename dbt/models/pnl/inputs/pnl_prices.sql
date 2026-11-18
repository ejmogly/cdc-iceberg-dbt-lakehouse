-- 손익 모델의 입력 pnl_prices(종목 x 거래일). Gold 시세 마트의 수정 전 가격입니다.
select symbol, trade_date, close_price, low_price, high_price
from {{ ref('mart_symbol_daily') }}
