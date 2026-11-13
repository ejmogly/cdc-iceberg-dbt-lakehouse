# Bronze, Silver, Gold

> Bronze 보존 방식은 3회차 확정, Silver 규칙은 제안(4회차 Silver 설계서에서 확정), Gold는 5회차 지표 정의서에서 정합니다.

## 1. 층마다 맡는 일

| 층 | 한 행 | 맡을 일 |
|---|---|---|
| Kafka 토픽 | 변경 이벤트 하나 | 원천 테이블 하나에 토픽 하나(`src.source.orders` 등). Kafka key는 원천 PK |
| Bronze | 변경 이벤트 하나 | 원문, topic, partition, offset, Kafka 시각, 적재 시각을 그대로 남김. `d`는 남기고 tombstone은 뺌. 모든 이력을 지우지 않음 |
| Silver | 키마다 현재 상태 하나 | 주문별 현재 상태, 계좌와 종목별 현재 보유, 종목과 거래일별 최신 시세. 삭제는 `is_deleted`로 표시 |
| Silver core | 사실(fact)과 차원(dim) | 여러 Gold 모델이 같이 쓰는 체결 사실, 종목 차원(seed) |
| Gold | 지표 하나의 집계 단위 | 아래 2절 |

## 2. Gold 지표 후보 (5회차에 2 ~ 3개를 골라 정의서로)

| 지표 | 집계 단위 | 정정, 삭제 때 기대 |
|---|---|---|
| 일별 종목별 체결 대금 | 시장 현지 거래일 x 종목 | 체결 정정이나 철회가 들어오면 그날 값만 다시 계산됨 |
| 주문 취소율 | 거래일 x 종목 | 취소가 늦게 들어와도 그날 값에 반영됨 |
| 잔고 대사 | 계좌 x 종목 | 체결 수량 합계와 Silver 잔고 수량이 같음. 전량 매도한 종목은 양쪽 모두 없음 |
| 평가금액 | 계좌 x 종목 x 날짜 | 시세가 정정되면 그날 평가금액만 바뀜 |
| 매매별 손익과 기간 상태 | 계좌 x 종목, 매매 한 건 | 손익 SQL 초안이 있음([`sql/pnl/`](../../sql/pnl/README.md), [0008](../decisions/0008-pnl-moving-average-in-sql.md)) |
| 「오늘의 시장」 | 미국 거래일 | 위험 쪽과 안전 쪽 종목의 오른 수, 상대 수익률, 평소와 다른 날, 주식과 채권 상관. 처음에는 두세 개만 |

「오늘의 시장」은 시장을 데이터로 읽는 지표입니다. 앞날을 맞히거나 매매를 권하지 않고, 관계는 "같이 움직였다"까지만 말합니다.

## 3. 연결 키

| 연결 | 키 |
|---|---|
| 체결 → 주문(계좌, 종목) | `executions.order_id = orders.order_id` |
| 잔고 대사 | (`account_id`, `symbol`) |
| 평가금액 → 시세 | (`symbol`, 기준일 당일 또는 그 이전의 마지막 `trade_date`) |
| 통화, 시장 | `instruments.csv`의 `symbol` |
| 미국 종목 원화 환산 | 체결 환율(`executions.fx_rate`), 비면 그날 기준율(`fx_rates`). 날짜 맞추는 규칙은 정할 것([0007](../decisions/0007-fx-rates-ecos-and-execution-rate.md)) |

거래일은 체결이면 `executed_at`을 그 시장의 현지 시각으로 바꾼 날짜, 시세면 `trade_date` 그대로입니다. 한국과 미국은 휴장일이 달라 일별 지표는 시장별로 나눠 셉니다.

## 4. Silver 현재 상태 규칙 (제안)

자세한 내용은 [0010](../decisions/0010-silver-current-state-rules.md)입니다.

- 최신 판단은 원천 LSN이 먼저이고, 같을 때만 Kafka offset을 봅니다.
- 삭제는 `is_deleted = true`로 표시하고, 유효한 행은 `WHERE NOT is_deleted`로 읽습니다.
- 삭제된 잔고 행의 `qty`, `avg_price`는 NULL입니다.
- 같은 이벤트를 두 번 받아도 Silver와 Gold가 그대로여야 합니다.

## 5. 층마다 확인할 것

| 층 | 확인 | 언제 |
|---|---|---|
| Kafka | 시나리오 뒤 토픽 메시지 수 6, 2, 3 | 3회차, 확인함 |
| Bronze | 시나리오 뒤 행 수 6, 2, 2. 파일 내용과 SQL 조회가 같음 | 4회차 |
| Silver | 원천과 건수, 값이 같음. 재전달과 작업 중단 뒤에도 같음 | 4회차 |
| Gold | `dbt build`의 데이터 테스트, 손익 골든 사례, 잔고 대사 | 5회차 |
| 증분 | 전체 재계산과 증분 결과가 같음 | 6회차 |
