-- 시나리오 여섯 단계(infra/postgres/scenario/01 ~ 06) 뒤 Silver의 기대값(docs/decisions/0010 「확인할 것」).
-- 하나라도 어긋나면 assert_true가 오류를 내고 멈춥니다. 시나리오와 다른 데이터가 더 들어 있으면 고쳐 씁니다.

-- 키마다 저장된 행은 하나
SELECT assert_true(count(*) = 0, 'orders: 같은 order_id가 두 행 이상') AS orders_key
FROM (SELECT order_id FROM lake.silver.orders GROUP BY order_id HAVING count(*) <> 1);
SELECT assert_true(count(*) = 0, 'executions: 같은 execution_id가 두 행 이상') AS executions_key
FROM (SELECT execution_id FROM lake.silver.executions GROUP BY execution_id HAVING count(*) <> 1);
SELECT assert_true(count(*) = 0, 'holdings: 같은 (account_id, symbol)이 두 행 이상') AS holdings_key
FROM (SELECT account_id, symbol FROM lake.silver.holdings GROUP BY account_id, symbol HAVING count(*) <> 1);
SELECT assert_true(count(*) = 0, 'daily_prices: 같은 (symbol, trade_date)가 두 행 이상') AS daily_prices_key
FROM (SELECT symbol, trade_date FROM lake.silver.daily_prices GROUP BY symbol, trade_date HAVING count(*) <> 1);
SELECT assert_true(count(*) = 0, 'fx_rates: 같은 (currency_pair, rate_date)가 두 행 이상') AS fx_rates_key
FROM (SELECT currency_pair, rate_date FROM lake.silver.fx_rates GROUP BY currency_pair, rate_date HAVING count(*) <> 1);

-- 잔고 (A-0001, 005930): 저장된 행 1개(삭제 표시), 유효한 행 0개, 수량과 평균 단가는 NULL
SELECT assert_true(
  count(*) = 1
  AND count_if(is_deleted) = 1
  AND count_if(NOT is_deleted) = 0
  AND count(qty) = 0 AND count(avg_price) = 0,
  'holdings (A-0001, 005930): 저장 1, 삭제 표시 1, 유효 0, qty와 avg_price NULL이어야 함') AS holding_deleted
FROM lake.silver.holdings
WHERE account_id = 'A-0001' AND symbol = '005930';

-- 주문 1001: 4주 체결 뒤 남은 6주 취소
SELECT assert_true(
  count(*) = 1 AND max(status) = 'CANCELLED' AND max(filled_qty) = 4
  AND max(limit_price) = 69500 AND NOT max(is_deleted),
  'order 1001: CANCELLED, filled_qty 4, limit_price 69500이어야 함') AS order_1001
FROM lake.silver.orders
WHERE order_id = 1001;

-- 주문 1002: 시장가 매도 4주 전량 체결
SELECT assert_true(
  count(*) = 1 AND max(status) = 'FILLED' AND max(filled_qty) = 4
  AND count(limit_price) = 0 AND NOT max(is_deleted),
  'order 1002: FILLED, filled_qty 4, limit_price NULL이어야 함') AS order_1002
FROM lake.silver.orders
WHERE order_id = 1002;

-- 주문 1001, 1002의 체결 두 건, 철회 없음(07부터의 증분 실험 체결은 빼고 셈)
SELECT assert_true(
  count(*) = 2 AND count_if(is_deleted) = 0
  AND sum(exec_qty * exec_price) = 558400,
  'executions(주문 1001, 1002): 2건, 철회 0, 대금 합 558,400이어야 함') AS executions_total
FROM lake.silver.executions
WHERE order_id IN (1001, 1002);
