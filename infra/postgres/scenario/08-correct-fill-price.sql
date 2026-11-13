-- 8단계: 지난 체결의 가격 정정(u). 체결 6001(11-09)의 가격이 750.00이 아니라 749.80이었음.
-- 영향: 체결 대금 (SPY, 11-09) 한 칸. 잔고 평균 단가도 다시 계산합니다.
-- 평균 단가 (10 x 749.80 + 5 x 748.50) / 15 = 11,240.5 / 15 = 749.3667(소수 넷째 자리)
BEGIN;
UPDATE source.executions
   SET exec_price = 749.80, updated_at = now()
 WHERE execution_id = 6001;
UPDATE source.holdings
   SET avg_price = 749.3667, updated_at = now()
 WHERE account_id = 'A-0002' AND symbol = 'SPY';
COMMIT;
