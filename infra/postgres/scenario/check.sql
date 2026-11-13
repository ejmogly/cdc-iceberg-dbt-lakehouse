-- 읽기만 합니다. 단계마다 원천의 현재 상태를 봅니다.
SELECT 'orders' AS tbl, count(*) FROM source.orders
UNION ALL
SELECT 'executions', count(*) FROM source.executions
UNION ALL
SELECT 'holdings', count(*) FROM source.holdings;

SELECT order_id, side, status, filled_qty,
       limit_price
  FROM source.orders ORDER BY order_id;

SELECT * FROM source.holdings;
