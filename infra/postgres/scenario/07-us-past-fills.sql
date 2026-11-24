-- 7단계: 계좌 A-0002가 미국 종목 SPY를 지난 두 거래일(뉴욕 11-09 월, 11-10 화)에 산 기록.
-- 증분 Gold 실험(docs/reports/2026-11-12_incremental-maintenance-airflow.md)용입니다. 01 ~ 06 뒤에 돌립니다.
-- 시각은 UTC로 직접 적습니다. 15:00 UTC는 뉴욕 10:00(정규장)입니다.
-- 가격은 그날 SPY 시세의 저가와 고가 사이, 환율은 그날 ECOS 매매기준율입니다(환율 출처: 한국은행 ECOS에서 제공된 정보).
-- 주문, 체결, 잔고를 거래마다 한 트랜잭션으로 바꿉니다.

-- 주문 2001: 지정가 751달러 10주, 750달러에 모두 체결(11-09)
BEGIN;
INSERT INTO source.orders
  (order_id, account_id, symbol, side, order_type,
   order_qty, limit_price, filled_qty, status,
   ordered_at, updated_at)
VALUES
  (2001, 'A-0002', 'SPY', 'BUY', 'LIMIT',
   10, 751, 10, 'FILLED',
   '2026-11-09 14:40:00+00', '2026-11-09 15:00:00+00');
INSERT INTO source.executions
  (execution_id, order_id, exec_qty, exec_price,
   fx_rate, executed_at, updated_at)
VALUES (6001, 2001, 10, 750.00, 1357.9,
  '2026-11-09 15:00:00+00', '2026-11-09 15:00:00+00');
INSERT INTO source.holdings
  (account_id, symbol, qty, avg_price, updated_at)
VALUES ('A-0002', 'SPY', 10, 750.00,
  '2026-11-09 15:00:00+00');
COMMIT;

-- 주문 2002: 시장가 5주, 748.50달러에 체결(11-10). 평균 단가 (7,500 + 3,742.5) / 15 = 749.5
BEGIN;
INSERT INTO source.orders
  (order_id, account_id, symbol, side, order_type,
   order_qty, limit_price, filled_qty, status,
   ordered_at, updated_at)
VALUES
  (2002, 'A-0002', 'SPY', 'BUY', 'MARKET',
   5, NULL, 5, 'FILLED',
   '2026-11-10 15:10:00+00', '2026-11-10 15:10:05+00');
INSERT INTO source.executions
  (execution_id, order_id, exec_qty, exec_price,
   fx_rate, executed_at, updated_at)
VALUES (6002, 2002, 5, 748.50, 1355.7,
  '2026-11-10 15:10:05+00', '2026-11-10 15:10:05+00');
UPDATE source.holdings
   SET qty = 15, avg_price = 749.5,
       updated_at = '2026-11-10 15:10:05+00'
 WHERE account_id = 'A-0002' AND symbol = 'SPY';
COMMIT;
