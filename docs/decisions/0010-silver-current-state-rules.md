# 0010 Silver 현재 상태 규칙

- 날짜: 2026-11-11
- 상태: 결정, 확인함(11/12 `sql/silver/*/2_merge.sql`로 구현, [첫 실행 보고서](../reports/2026-11-12_lake-bronze-silver-gold.md) 2절)

## 맥락

Bronze에는 같은 키의 변경이 여러 번 쌓입니다(주문 하나가 접수, 정정, 부분 체결, 취소로 네 번).
Silver는 키마다 현재 상태 한 행을 가져야 합니다. 그런데 Kafka는 같은 이벤트를 다시 보낼 수 있고, 재전달된 옛 변경은 새 offset을 달고 옵니다.
삭제는 행을 지울지 표시만 할지 골라야 합니다.

## 결정

1. **묶는 키**: `orders`는 `order_id`, `executions`는 `execution_id`, `holdings`는 (`account_id`, `symbol`), `daily_prices`는 (`symbol`, `trade_date`), `fx_rates`는 (`currency_pair`, `rate_date`).
2. **최신 판단**: 같은 키 안에서 원천 LSN(`source.lsn`)을 먼저 보고, LSN이 같을 때만 Kafka offset으로 가립니다. `updated_at`은 한 트랜잭션 안에서 같은 값이 겹치므로 보조로만 씁니다.
3. **삭제**: 최신 이벤트가 `d`면 행을 지우지 않고 `is_deleted = true`로 표시하고, `source_lsn`을 그 삭제의 LSN으로 바꿉니다. 유효한 행은 `WHERE NOT is_deleted`로 읽습니다.
4. **삭제된 잔고**: `qty`, `avg_price`는 NULL로 둡니다. 삭제 이벤트의 값은 채움 값이기 때문입니다([0005](0005-replica-identity-executions-full.md)).
5. **다시 산 종목**: 같은 키의 `c`가 더 큰 LSN으로 오면 `is_deleted = false`와 새 값으로 바뀝니다.
6. **재전달**: 같은 이벤트를 두 번 받아도 Silver와 Gold가 그대로여야 합니다. LSN 조건으로 막고, 중복은 (topic, partition, offset)으로 가려냅니다.

## 검토한 다른 방법

| 방법 | 고르지 않은 이유 |
|---|---|
| Kafka offset만으로 최신 판단 | 재전달된 옛 변경이 새 offset으로 와서 최신처럼 보일 수 있음 |
| `updated_at`으로 최신 판단 | 한 트랜잭션의 여러 변경이 같은 시각을 가짐 |
| 삭제 때 행을 물리 삭제 | 삭제가 늦게 도착한 옛 변경보다 먼저 처리되면 옛 값이 되살아날 수 있음. 표시를 남기면 LSN으로 막을 수 있음 |

## 확인할 것 (4회차)

시나리오 여섯 단계(`infra/postgres/scenario/`) 뒤에 Silver에서 아래가 나와야 합니다.

| 키 | 기대 |
|---|---|
| 잔고 (`A-0001`, `005930`) | 저장된 행 1개(`is_deleted = true`), 유효한 행 0개 |
| 주문 1001 | `CANCELLED`, `filled_qty` 4 |
| 주문 1002 | `FILLED`, `filled_qty` 4 |

같은 이벤트를 다시 보내고, Flink 작업을 중간에 멈췄다 다시 돌린 뒤에도 같은 결과여야 합니다.
