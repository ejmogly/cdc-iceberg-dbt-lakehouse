-- 잔고 대사: 체결 순수량과 Silver 유효 잔고 수량이 계좌, 종목마다 같아야 합니다.
-- 어긋난 행이 나오면 실패입니다. 체결과 잔고는 원천에서 한 트랜잭션이지만 Silver는 테이블마다
-- 따로 commit하므로(가이드 04 12-7), 적재 도중에 돌리면 잠깐 어긋날 수 있습니다.
select account_id, symbol, executed_net_qty, holding_qty, qty_diff
from {{ ref('mart_holding_reconciliation') }}
where qty_diff <> 0
