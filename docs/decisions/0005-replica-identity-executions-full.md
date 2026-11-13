# 0005 REPLICA IDENTITY는 체결 테이블만 FULL

- 날짜: 2026-11-11
- 상태: 결정

## 맥락

PostgreSQL의 기본값(`REPLICA IDENTITY DEFAULT`)에서는 변경 이벤트의 `before`가 비어 있거나 일부만 옵니다. 2회차에 이 저장소에서 직접 확인했습니다.

| 이벤트 | DEFAULT에서 `before` | FULL에서 `before` |
|---|---|---|
| 정정(`u`) | 없음(null) | 바뀌기 전 행 전체 |
| 삭제(`d`) | 키만 진짜. 키가 아닌 NOT NULL 칸은 빈 문자열과 0으로 채워짐 | 지워지기 전 행 전체 |

체결이 철회(삭제)되면, 그 체결이 어느 주문의 것이었는지(`order_id`) 알아야 다시 계산할 계좌, 종목, 날짜를 찾을 수 있습니다(6회차 영향 범위).
DEFAULT에서는 삭제 이벤트에 `execution_id`만 옵니다.

## 결정

1. `executions`만 `REPLICA IDENTITY FULL`로 둡니다.
2. `orders`, `holdings`, `daily_prices`는 기본값(DEFAULT)으로 둡니다.
3. Silver는 삭제 이벤트의 `before` 값을 믿지 않고 `op = 'd'`만 보고 삭제로 처리합니다. 바로 앞 값이 필요하면 Bronze에서 같은 키의 이전 이벤트 `after`를 봅니다.

## 검토한 다른 방법

| 방법 | 고르지 않은 이유 |
|---|---|
| 모두 DEFAULT, 철회 때 Bronze 이력에서 `order_id`를 찾음 | 가능하지만, 증분 처리마다 이력을 거슬러 찾는 조인이 붙음. 삭제 이벤트 하나로 영향 범위를 알 수 있는 쪽이 단순함 |
| 모두 FULL | 필요 없는 테이블까지 WAL이 커짐. `orders`는 정정이 잦아 늘어나는 양이 큼 |

## 결과

- FULL은 UPDATE와 DELETE 때 이전 행 전체를 WAL에 남깁니다. 체결은 거의 추가만 있어서 늘어나는 양이 작습니다.
- `holdings` 삭제 이벤트에서 `qty = 0`처럼 보이는 값은 진짜 값이 아닙니다. Silver의 삭제된 잔고 행은 `qty`, `avg_price`를 NULL로 둡니다([0010](0010-silver-current-state-rules.md)).
- 설정은 [`infra/postgres/init/10_brokerage.sql`](../../infra/postgres/init/10_brokerage.sql)의 `executions` 정의 바로 아래에 있습니다.
