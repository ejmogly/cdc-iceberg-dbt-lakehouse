-- 종목 x 거래일(그 시장의 현지 날짜) = 1행. 체결 건수, 수량, 대금.
-- 대금은 종목 통화 그대로이고 원화 환산, 수수료와 세금 반영 전입니다. 회계 손익이 아닙니다.
-- fct_execution 하나만 묶으므로 1 대 N 조인이 없습니다(fan-out 없음).
-- 입고(order_type TRANSFER_IN, 데이터 기간 전 보유를 첫날에 넣은 것)는 매매가 아니라 뺍니다(결정 기록 0013, 2026-11-24).
-- sql/gold_incremental/run.py의 inc_full이 같은 조건입니다.
select
  symbol,
  market,
  currency,
  trade_date,
  count(*) as execution_count,
  sum(exec_qty) as traded_qty,
  sum(exec_value) as traded_value
from {{ ref('fct_execution') }}
where order_type is distinct from 'TRANSFER_IN'
group by symbol, market, currency, trade_date
