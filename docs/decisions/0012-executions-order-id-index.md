# 0012 원천 체결 테이블의 order_id에 인덱스를 둔다

- 날짜: 2026-11-18
- 상태: 결정(담당자 「정함으로(넣기)」), 확인함

## 맥락

[규모 시험](../reports/2026-11-18_scale-test.md) 4절에서 합성 매매 26만 건을 지우는 데 19분이 걸렸습니다. 원천 `source.executions`는 `order_id`로 `source.orders`를 가리키는 외래 키가 있지만, `order_id`에는 인덱스가 없었습니다(기본 키 `execution_id`만). 그래서 주문 한 건을 지울 때마다 PostgreSQL이 그 주문을 가리키는 체결이 있는지 체결 테이블을 처음부터 훑었습니다. 5만 건으로 견주면 인덱스 없이 36.6초, 임시 인덱스를 두면 0.62초였습니다.

## 결정

1. `source.executions (order_id)`에 인덱스 `executions_order_id_idx`를 둡니다.
2. 새로 만드는 DB는 [`infra/postgres/init/10_brokerage.sql`](../../infra/postgres/init/10_brokerage.sql)이 만들고, 이미 떠 있는 DB는 [`infra/postgres/migrate/0012_executions_order_id_index.sql`](../../infra/postgres/migrate/0012_executions_order_id_index.sql)을 `make source-migrate`로 넣습니다(`CREATE INDEX CONCURRENTLY IF NOT EXISTS`, 여러 번 돌려도 같음).

## 영향

- CDC: 인덱스는 DDL이라 Debezium(pgoutput)이 변경 이벤트를 내지 않습니다. Bronze와 Silver에는 바뀌는 것이 없습니다.
- 쓰기: 체결을 넣을 때 인덱스 하나를 더 고칩니다. 5만 건 넣기는 인덱스가 있을 때도 2.5초인 회차가 있어, 이 인덱스가 넣기를 눈에 띄게 늦추지는 않는 것으로 봅니다(해석, 아래 「남은 것」).

## 검토한 다른 방법

| 방법 | 고르지 않은 이유 |
|---|---|
| 그대로 두기 | 주문을 지우는 일(정정, 시험 정리)이 체결 수에 비례해 느려짐 |
| 지울 때만 임시 인덱스 | 지우는 쪽마다 기억해야 하고, 원천 앱의 평소 지우기는 그대로 느림 |
| 외래 키를 없애기 | 원천의 정합성(없는 주문의 체결)을 잃음 |

## 확인한 것 (2026-11-18)

| 경우 | 결과 |
|---|---|
| `make source-migrate` 두 번 | 처음에 만들고, 두 번째는 「이미 있음」으로 건너뜀 |
| 5만 건 지우기(인덱스를 둔 뒤 세 번) | 0.47, 0.49, 0.43초(전에는 36.6초) |
| 지움이 Bronze에 닿기까지 | 5만 건 모두, total p95 10.0초 |
| Silver 증분 읽기 뒤 전체 재계산과 비교 | 다섯 테이블 0 / 0 |

## 남은 것

5만 건 넣기가 2.4 ~ 2.5초인 회차와 32 ~ 36초인 회차로 갈렸습니다(여섯 번 가운데 셋이 느림). 인덱스가 없을 때도 있을 때도 느린 회차가 있었고, 지운 행을 정리(`VACUUM`)한 뒤에도 느렸습니다. 대기 종류를 찍은 회차는 빨라서 원인을 찾지 못했습니다.
