# 결정 기록

"왜 이렇게 정했나"를 남깁니다. 한 파일에 결정 하나이고, 맥락, 결정, 검토한 다른 방법, 결과 순서로 씁니다.
결정을 바꾸면 파일을 지우지 않고 상태를 고친 뒤 새 번호로 씁니다.

| 번호 | 결정 | 날짜 | 상태 |
|---|---|---|---|
| [0001](0001-local-runtime.md) | 로컬 실행 환경: Colima, profile로 나눈 compose 하나, Connect 이미지 직접 빌드 | 2026-11-04 | 결정 |
| [0002](0002-lake-versions.md) | Flink, Iceberg, 카탈로그, Spark, S3 호환 저장소, dbt 버전 조합 | 2026-11-12 | 결정 |
| [0003](0003-synthetic-trades-public-prices.md) | 주문, 체결, 잔고는 합성 데이터, 일별 시세만 실제 공공 데이터 | 2026-11-06 | 결정 |
| [0004](0004-numeric-types-and-decimal-handling.md) | 가격은 `numeric(18,4)`, 커넥터는 `decimal.handling.mode=string` | 2026-11-11 | 결정 |
| [0005](0005-replica-identity-executions-full.md) | REPLICA IDENTITY는 체결 테이블만 FULL | 2026-11-11 | 결정 |
| [0006](0006-batch-prices-through-cdc.md) | 배치 데이터도 원천 DB를 거쳐 CDC로, 값이 바뀐 행만 고침 | 2026-11-11 | 결정, PostgreSQL 확인 남음 |
| [0007](0007-fx-rates-ecos-and-execution-rate.md) | 환율은 ECOS 매매기준율, 체결마다 환율 칸 | 2026-11-11 | 결정, 날짜 맞추는 규칙은 정할 것 |
| [0008](0008-pnl-moving-average-in-sql.md) | 손익은 이동평균법으로, 모델이 아니라 SQL이 계산 | 2026-11-11 | 결정 |
| [0009](0009-commit-evaluation-before-development.md) | AI 기능의 평가 기준은 개발 전에 커밋 | 2026-11-06 | 결정 |
| [0010](0010-silver-current-state-rules.md) | Silver 현재 상태: LSN으로 최신 판단, 삭제는 표시 | 2026-11-11 | 제안(4회차) |
| [0011](0011-silver-incremental-read.md) | Silver는 Bronze의 새 스냅샷만 읽는다(증분 읽기) | 2026-11-18 | 결정, 확인함 |
| [0012](0012-executions-order-id-index.md) | 원천 체결 테이블의 order_id에 인덱스를 둔다 | 2026-11-18 | 결정, 확인함 |
| [0013](0013-screen-accounts-and-transfer-in.md) | 화면 매매를 증권사마다 계좌로 나누고, 데이터 기간 전 보유는 입고 주문으로 넣는다 | 2026-11-19 | 결정, 확인함 |
| [0014](0014-portfolio-tab-and-account-mart.md) | 전체 탭은 종목마다 같은 계산을 더하고, Gold에는 계좌별 손익 마트를 둔다 | 2026-11-19 | 결정, 확인함 |
| [0015](0015-extend-data-window-keep-eval-frozen.md) | 데이터 기간은 원본 구간을 뒤에 더해 늘리고, 평가는 정답을 고정한 때의 얼린 파일로 한다 | 2026-11-19 | 결정, 확인 중 |

## 다음에 쓸 것

| 무엇 | 언제 |
|---|---|
| 메시지 형식: JSON(스키마 없음) 유지 또는 Avro와 Schema Registry | 4회차 전(2회차에 정하지 못함) |
| 미국 수정주가, 미국 거래일과 환율 날짜 맞추기, 처음 넣을 시세 기간 | 4회차 전([`docs/ai/data-prep.md`](../ai/data-prep.md) 9절) |
| Gold 지표와 갱신 주기 | 5회차 |
| 증분 처리와 소급 정정 범위 | 6회차 |
| 스냅샷 보존 기간과 파일 합치기 기준 | 7회차 |
