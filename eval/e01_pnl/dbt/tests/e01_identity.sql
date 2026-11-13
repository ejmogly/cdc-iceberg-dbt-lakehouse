-- E01 항등식: 실현 + 평가 = 합계, 가격 + 환율 + 교차 = 합계를 반올림 전 값으로(오차 0.01원 미만).
-- 정답이 필요 없어 골든과 규모 실행 모두에서 돕니다.
select position_id, gap_realized_unrealized, gap_effects
from {{ ref('pnl_position') }}
where abs(gap_realized_unrealized) >= 0.01
   or abs(gap_effects) >= 0.01
