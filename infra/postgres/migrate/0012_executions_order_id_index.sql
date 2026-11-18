-- 이미 떠 있는 원천 DB에 체결 order_id 인덱스를 넣습니다(docs/decisions/0012).
-- 새로 만드는 DB는 init/10_brokerage.sql이 같은 인덱스를 만듭니다. 여러 번 돌려도 같습니다.
-- CONCURRENTLY라 만드는 동안에도 쓰기를 막지 않습니다(트랜잭션 밖에서 돌려야 함, psql 기본 자동 커밋).
-- 인덱스는 DDL이라 Debezium(pgoutput)이 변경 이벤트를 내지 않습니다.
CREATE INDEX CONCURRENTLY IF NOT EXISTS executions_order_id_idx ON source.executions (order_id);
