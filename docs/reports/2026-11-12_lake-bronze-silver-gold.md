# Bronze, Silver, Gold 첫 실행 (2026-11-12)

증권 앱 원천 네 테이블(`orders`, `executions`, `holdings`, `daily_prices`)을 이 저장소의 환경에서 처음으로 Gold까지 돌린 기록입니다.
1 ~ 4절은 네 테이블로 돌린 결과이고, 같은 날 환율(`fx_rates`)을 다섯째 테이블로 더한 결과는 5절에 있습니다.
그 뒤의 증분, 유지보수, Airflow(시나리오 07 ~ 09 포함)는 [다음 보고서](2026-11-12_incremental-maintenance-airflow.md)에 있습니다. 그래서 지금 Bronze 행 수는 1절의 6, 2, 2가 아니라 9, 6, 5입니다.
시나리오 여섯 단계(`infra/postgres/scenario/01 ~ 06`) 뒤의 값으로 층마다 기대값을 확인했습니다.

## 요약

| 층 | 확인 | 결과 |
|---|---|---|
| Bronze | 행 수 orders 6, executions 2, holdings 2(tombstone 제외) | 같음 |
| Bronze | 작업을 멈췄다 다시 시작(savepoint에서, Kafka 읽은 위치에서), 카탈로그 DB 장애 뒤 자동 복구 | 세 경우 모두 행 수 그대로, 전달 위치 중복 0 |
| Silver | [0010](../decisions/0010-silver-current-state-rules.md) 「확인할 것」 표 | 같음 |
| Silver | 같은 입력 다시 넣기, 과거 변경 다시 넣기 | 네 테이블 모두 바뀐 행 0 |
| Gold | `dbt build`(seed 2, 모델 4, 데이터 테스트 46) | 52개 모두 통과 |
| Gold | 가이드 05 12절 ④의 손 계산(5단계 뒤 2건, 8주, 558,400원) | 같음 |
| 전체 | 레이크만 지우고 처음부터 Gold까지 다시 만들기(세 번) | 세 번 모두 같은 값, 중앙값 58초(4절) |
| 시세 | 원천에 실제 시세 4,736행을 넣은 뒤 Bronze, Silver | 원천과 종목별 행 수, 기간, 종가 합, 거래량 합이 같음(5절) |
| 환율 | 다섯째 테이블로 더한 뒤 Bronze, Silver, `dbt build` | 245행이 원천과 같음. 다섯 테이블 검증과 `dbt build` 55개 통과(5절) |

## 측정 조건

| 항목 | 값 |
|---|---|
| 기기 | Apple M3 Pro, 메모리 36GB, macOS 15.5 |
| 컨테이너 | Colima 0.10.3, VM CPU 6, 메모리 12GiB, 디스크 60GiB, Docker 29.5.2, Compose 5.7.0 |
| 원천과 CDC | `postgres:16.15`, `apache/kafka:4.3.2`(토픽마다 파티션 1개), Debezium PostgreSQL 3.6.3(pgoutput, 스키마 없는 JSON, `decimal.handling.mode=string`) |
| 레이크 | 버전은 [0002](../decisions/0002-lake-versions.md). Flink 1.20.4, Iceberg 1.10.0(JDBC 카탈로그), Spark 3.5.6, dbt-core 1.12.5와 dbt-spark 1.9.3 |
| Flink | 병렬도 1, checkpoint 10초, EXACTLY_ONCE, Kafka 읽기 시작 위치는 group offset(없으면 earliest) |
| Spark | `local[2]`, 드라이버 메모리 2g, 세션 시간대 UTC, ANSI 모드, Thrift 세션 하나를 모두가 같이 씀 |
| Iceberg 테이블 | format-version 2, 쓰기 방식 기본값(copy-on-write), 파티션 없음 |
| 데이터 | 합성 데이터. 계좌 `A-0001`, 종목 `005930` 하나로 주문 2건, 체결 2건, 잔고 1건(마지막에 삭제). 시나리오는 2026-11-11에 원천에 넣었고 Kafka 메시지는 6, 2, 3개 |
| `daily_prices` | 1 ~ 4절은 원천에 행이 없어 토픽도 없던 때(Bronze, Silver 0행). 5절은 공개 시세 4,736행을 넣은 뒤 |

## 실행 명령

처음 한 번은 `.env.example`을 `.env`로 복사해 값을 바꾸고, 원천 테이블과 시나리오는 가이드 03의 11-9대로 넣습니다.

```bash
colima start --cpu 6 --memory 12 --disk 60
docker compose up -d
infra/connect/register.sh
docker compose --profile flink \
  --profile spark up -d
infra/flink/submit-bronze.sh
infra/spark/silver.sh
infra/spark/check-silver.sh
docker compose --profile spark \
  run --rm dbt build
```

| 명령 | 하는 일 |
|---|---|
| `submit-bronze.sh` | [`infra/flink/bronze.sql`](../../infra/flink/bronze.sql)을 Flink에 보냄. 이미 돌고 있으면 그대로 둠 |
| `stop-bronze.sh` | savepoint를 남기고 멈춤. 찍힌 경로를 `submit-bronze.sh <경로>`에 주면 그 자리에서 다시 시작 |
| `silver.sh` | 테이블마다(5절부터 다섯 개) [`sql/silver/<테이블>/1_incoming.sql`](../../sql/silver/)(검사와 최신 후보)과 `2_merge.sql`(MERGE)을 한 세션에서 돌림 |
| `check-silver.sh` | 2절의 세 가지 검증 |
| `sql.sh` | Thrift server에 SQL 파일이나 한 줄 SQL을 보냄 |
| `dbt build` | [`dbt/`](../../dbt/dbt_project.yml)의 seed, 모델, 테스트 |

끌 때는 아래 두 줄입니다. 볼륨은 남아 다음에 이어집니다.

```bash
docker compose --profile flink \
  --profile spark stop
colima stop
```

화면: Kafka UI `127.0.0.1:18080`, Flink `127.0.0.1:18081`, Spark `127.0.0.1:14040`, 저장소 콘솔 `127.0.0.1:19001`.

## 1. Bronze

Flink 작업 하나가 토픽 패턴 `src\.source\.(orders|executions|holdings|daily_prices)`를 읽어 `lake.bronze.<테이블>` 네 개에 씁니다.
칸은 `payload`(원문), `topic`, `partition_id`, `kafka_offset`, `kafka_ts`, `ingested_at`이고, 값이 없는 tombstone은 행이 되지 않습니다.

| 테이블 | 기대 | 결과 | op |
|---|---:|---:|---|
| `orders` | 6 | 6 | c, u, u, u, c, u |
| `executions` | 2 | 2 | c, c |
| `holdings` | 2 | 2 | c, d |
| `daily_prices` | 0 | 0 | |

- 작업을 넣은 지 10초 안팎(checkpoint 한 번) 뒤에 세 테이블에 snapshot이 하나씩 생겼고, 데이터 파일은 테이블마다 Parquet 하나입니다.
- 새 변경이 없어도 snapshot은 늘어납니다. `orders`는 checkpoint 1에서 6행을 더한 뒤, checkpoint 11에서 행 없는 `append` snapshot이 하나 더 생겼습니다(약 100초 뒤). 빈 checkpoint가 10번 이어지면 한 번 commit하는 Iceberg Flink 쓰기의 기본 동작으로 보입니다(설정 `flink.max-continuous-empty-commits`, 확인 전). 10초 주기면 테이블마다 하루 약 860개라, 7회차의 snapshot 정리 대상입니다.
- 행마다 `source.lsn`이 원천 순서대로 커집니다. 예: 잔고 `c` 27424264, `d` 27433432.
- 잔고 삭제(`d`)의 `before`는 `qty` 0, `avg_price` `"0.0000"`, `updated_at` 1970-01-01입니다. REPLICA IDENTITY DEFAULT의 채움 값이라 Silver에서 NULL로 바꿉니다([0005](../decisions/0005-replica-identity-executions-full.md)). 체결은 FULL이라 `before`에 진짜 값이 있습니다.

멈췄다 다시 시작해도 행이 늘지 않는지 봤습니다.

| 상황 | 다시 시작한 방법 | 행 수 | (topic, partition, offset) 중복 |
|---|---|---|---:|
| savepoint를 남기고 멈춤 | 그 savepoint에서 | 6, 2, 2 | 0 |
| savepoint 없이 취소 | Kafka에 commit된 읽은 위치에서 | 6, 2, 2 | 0 |
| 카탈로그 DB 재시작으로 commit 실패(6절 문제 1) | Flink가 마지막 checkpoint에서 스스로 복구(`restored` 1) | 6, 2, 2 | 0 |

## 2. Silver

규칙은 [0010](../decisions/0010-silver-current-state-rules.md)과 가이드 04 12-4, 12-5입니다.
키마다 LSN을 숫자로 먼저 보고 같으면 offset으로 최신 후보 하나를 고르고, `MERGE`의 `WHEN MATCHED`에만 버전 가드를 둡니다.
삭제는 `is_deleted = true`로 남기고, 잔고 삭제의 `qty`, `avg_price`는 NULL입니다.
MERGE 전에 키 없음, LSN 없음, 모르는 op, 같은 전달 위치에 다른 원문, 꼭 있어야 할 값 없음을 검사해 하나라도 있으면 멈춥니다. 글자를 숫자로 바꾸다 실패해도 ANSI 모드라 멈춥니다.

**기대값** ([`sql/silver/checks/expected.sql`](../../sql/silver/checks/expected.sql)): 통과.

| 키 | 기대 | 결과 |
|---|---|---|
| 잔고 (`A-0001`, `005930`) | 저장된 행 1개(`is_deleted = true`), 유효한 행 0개 | 같음. `qty`, `avg_price` NULL, `source_lsn` 27433432(삭제의 LSN) |
| 주문 1001 | `CANCELLED`, `filled_qty` 4 | 같음. `limit_price` 69,500 |
| 주문 1002 | `FILLED`, `filled_qty` 4 | 같음. `limit_price` NULL(시장가) |
| 체결 | 2건, 철회 0 | 같음. 대금 합 558,400 |
| 원천과 비교 | 원천 orders 2, executions 2, holdings 0 | Silver 유효 행 2, 2, 0 |

**같은 입력 다시 넣기, 과거 변경 다시 넣기** ([`infra/spark/check-silver.sh`](../../infra/spark/check-silver.sh)): 직전 snapshot과 지금 사이에 한쪽에만 있는 행(모든 칸 비교)을 셌습니다.

| 시험 | 바뀐 행(orders, executions, holdings, daily_prices) | snapshot 수 |
|---|---|---|
| 같은 입력으로 네 테이블 MERGE 한 번 더 | 0, 0, 0, 0 | 네 테이블 모두 하나씩 늘어남 |
| 주문 1001의 첫 `c`(ACCEPTED, 70,000원)와 잔고 첫 매수 `c`(4주)를 offset + 1000으로 MERGE | 0, 0, 0, 0 | orders, holdings만 하나씩 늘어남 |

- 값은 그대로인데 snapshot은 늘었습니다. copy-on-write `MERGE`가 짝이 맞은 행이 든 파일을 다시 쓰기 때문입니다(가이드 04 10-3). 빈 `daily_prices`도 한 번 MERGE할 때마다 snapshot이 하나 생겼습니다. 주기적으로 돌리면 7회차의 snapshot 정리가 필요합니다.
- **대조 시험**: 같은 옛 `c`를 가드 없는 MERGE로 임시 복사본에 넣었더니 잔고가 4주, `is_deleted = false`로 되살아났습니다(복사본은 지움). 가드가 있어야만 막힌다는 뜻이라, 위 시험이 헛돌지 않습니다.

## 3. Gold

[`dbt/`](../../dbt/dbt_project.yml)는 가이드 05 12절 설계를 옮긴 것입니다. Silver 현재 상태 네 테이블은 dbt의 source이고, dbt는 seed와 Silver core(`dim_order`, `fct_execution`, `lake.silver`), Gold(`lake.gold`)를 만듭니다.

| 종류 | 이름 |
|---|---|
| seed | `instruments`(원본 `loaders/instruments.csv`를 가리키는 링크), `markets`(시장별 시간대) |
| Silver core | `dim_order`(주문 1건), `fct_execution`(철회 뺀 체결 1건, 거래일은 그 시장의 현지 날짜) |
| Gold | `mart_daily_symbol_trading`(종목 x 거래일), `mart_holding_reconciliation`(계좌 x 종목 잔고 대사) |
| 일반 테스트 | `not_null`, `unique`, `relationships`(체결 → 주문, 주문 → 종목, 종목 → 시장), `accepted_values`(방향, 상태, 통화) |
| singular 테스트 | 주말 거래일 없음, 대금 합(통화별 core = Gold), 종목 x 거래일 한 행, 잔고 대사 차이 0, 주문별 체결 수량 = `filled_qty` |

`dbt build` 결과: `PASS=52 WARN=0 ERROR=0`, 6.6초.

| Gold | 기대(가이드 05 12절 ④, 5단계 뒤) | 결과 |
|---|---|---|
| `005930`, 2026-11-11, 체결 건수 | 2 | 2 |
| 체결 수량 | 8 | 8 |
| 체결 대금(원) | 558,400 | 558,400 |
| 잔고 대사 (`A-0001`, `005930`) | 체결 순수량 0, 잔고 0, 차이 0 | 같음 |

- 체결 시각은 UTC 06:47(서울 15:47)이라 거래일은 서울 날짜 2026-11-11(수)입니다.
- 주문 1001이 취소됐어도 이미 체결된 4주는 대금에 들어갑니다(가이드 05 12절 ③).

## 4. 처음부터 다시 만들기

원천 PostgreSQL과 Kafka는 그대로 두고, 레이크 볼륨 세 개(카탈로그 DB, 저장소, Flink 상태)만 지운 뒤 Gold까지 다시 만들었습니다.
이미지는 이미 빌드된 상태입니다. Kafka에는 Flink가 commit한 읽은 위치가 남으므로 먼저 처음으로 되돌립니다.

```bash
infra/flink/stop-bronze.sh
docker compose --profile flink \
  --profile spark rm -sf jobmanager \
  taskmanager spark s3-init s3 catalog-db
docker volume rm cdclake_catalog-data \
  cdclake_lake-data cdclake_flink-state
docker compose exec -T kafka \
  /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group flink-bronze-brokerage \
  --reset-offsets --to-earliest \
  --all-topics --execute
```

그 뒤 「실행 명령」의 `up -d`부터 `dbt build`까지를 돌려 단계마다 초를 쟀습니다(`up -d --wait`, 1초 단위, 세 번).

| 단계 | 1회 | 2회 | 3회 | 중앙값 |
|---|---:|---:|---:|---:|
| 레이크 서비스 올리기(`up -d --wait`) | 18 | 18 | 18 | 18 |
| Bronze 작업 보내기(`submit-bronze.sh`) | 9 | 9 | 10 | 9 |
| 작업을 보낸 뒤 Bronze 6, 2, 2가 보일 때까지 | 9 | 13 | 6 | 9 |
| Silver 네 테이블(`silver.sh`) | 9 | 9 | 9 | 9 |
| Gold(`dbt build`) | 13 | 13 | 13 | 13 |
| 합계 | 58 | 62 | 56 | 58 |

- 세 번 모두 Bronze 6, 2, 2, `dbt build` 52개 통과였고, 마지막 상태에서 `check-silver.sh`도 통과했습니다.
- Bronze가 보이기까지의 시간이 6 ~ 13초로 흔들리는 것은 checkpoint 주기(10초) 안에서 작업이 언제 시작했는지에 따라 첫 commit 시각이 달라지기 때문입니다.
- `dbt build` 13초 가운데 dbt 자체 실행은 약 6.5초이고, 나머지는 컨테이너를 띄우고 Thrift에 붙는 시간입니다.
- 다 띄운 상태의 메모리(`docker stats`)는 합계 약 4.3GiB였습니다. Spark 1.27GiB, Kafka 0.95GiB, taskmanager 0.66GiB, jobmanager 0.42GiB, Connect 0.41GiB, Kafka UI 0.38GiB, 저장소 0.18GiB, PostgreSQL 두 개 합 0.07GiB입니다. VM 12GiB 안에서 여유가 있습니다.

## 5. 실제 시세와 환율을 넣은 뒤

11:25 ~ 11:35에 원천 `source.daily_prices`에 공개 시세를 넣었습니다([0003](../decisions/0003-synthetic-trades-public-prices.md), 적재는 `loaders/daily_prices.py`, 수정주가 아님).
국내 2종목은 243거래일, 미국 17종목은 250거래일씩, 모두 4,736행입니다. 같은 적재를 한 번 더 돌렸을 때 고친 행은 0이었고, Kafka 토픽 `src.source.daily_prices`의 끝 위치도 4,736 그대로였습니다.
같은 때 원천 `source.fx_rates`에 환율 245행(ECOS 원/달러 매매기준율, [0007](../decisions/0007-fx-rates-ecos-and-execution-rate.md))도 들어갔습니다.

### 5-1. 시세

그 뒤 Flink를 다시 올려 Bronze 작업을 Kafka에 commit된 읽은 위치에서 이어 시작했습니다.

| 확인 | 결과 |
|---|---|
| Bronze `daily_prices` | 작업을 보낸 지 20초 안에 4,736행, 모두 `c`, offset 0 ~ 4735, 전달 위치 중복 0. 처음 보는 토픽이라 처음부터 읽음 |
| Bronze의 다른 세 테이블 | 6, 2, 2 그대로(멈춘 사이 새 이벤트 없음, 중복 없음) |
| Silver `daily_prices` MERGE | 5초, 입력 검사(빈 값, 저가와 고가 범위) 통과 |
| Silver와 원천 대조 | 종목 19개 모두 행 수, 첫 거래일과 마지막 거래일(2025-11-12, 2026-11-10), 종가 합, 거래량 합이 같음. 정수 날짜 변환도 맞음 |
| `check-silver.sh` | 통과. 같은 입력으로 다시 MERGE해도 4,736행 가운데 바뀐 행 0 |
| `dbt build` | `PASS=52`(시세 source 테스트 포함) |

### 5-2. 환율을 다섯째 테이블로

처음 범위(원천 네 테이블)에 없던 환율을 레이크에 더했습니다(11/12 12:05에 정함).
[`bronze.sql`](../../infra/flink/bronze.sql)의 토픽 패턴과 INSERT에 `fx_rates`를 더하고, [`sql/silver/fx_rates/`](../../sql/silver/fx_rates/1_incoming.sql)를 시세와 같은 규칙으로 썼습니다.
키는 (`currency_pair`, `rate_date`), `rate_date`는 정수 날 수에서 날짜로, `rate`는 글자에서 `DECIMAL(12,4)`로 바꾸고, 환율이 비었거나 0 이하이면 멈춥니다.
작업 모양이 바뀌어 savepoint 없이 Bronze 작업을 다시 보냈습니다.

| 확인 | 결과 |
|---|---|
| Bronze `fx_rates` | 작업을 보낸 지 21초 안에 245행, 전달 위치 중복 0. 처음 보는 토픽이라 처음부터 읽음 |
| Bronze의 다른 네 테이블 | 6, 2, 2, 4,736 그대로. 이미 읽은 토픽은 commit된 위치부터 이어 읽어 중복 없음 |
| Silver와 원천 대조 | `USDKRW` 245행, 2025-11-12 ~ 2026-11-11, 환율 합 355,167.7, 최솟값 1,322.6, 최댓값 1,554.4로 같음 |
| `check-silver.sh` | 다섯 테이블 모두 통과. 같은 입력으로 다시 MERGE해도 바뀐 행 0 |
| `dbt build` | `PASS=55`(환율 source 테스트 3개 추가) |

환율은 아직 Gold 모델이 쓰지 않습니다. 미국 거래일과 환율 날짜를 맞추는 규칙은 0007에서 정할 일로 남아 있습니다.

## 6. 겪은 문제와 고친 것

| 문제 | 원인 | 고친 것 |
|---|---|---|
| 1. 검증을 몇 번 돌리자 Spark가 카탈로그에 붙지 못함(`too many clients already`) | Thrift 접속마다 Spark 세션을 새로 만들게 했더니, 세션마다 Iceberg JDBC 카탈로그가 카탈로그 DB 연결을 새로 열고 닫지 않아 PostgreSQL 한도 100을 넘김 | `singleSession=true`로 세션 하나를 같이 씀. 그 뒤 반복해도 연결 6개 그대로. 대신 Silver와 dbt를 동시에 돌리지 않음([`start-thrift.sh`](../../infra/spark/start-thrift.sh)) |
| 2. Silver의 `SET`이 같이 쓰는 세션으로 dbt에 샐 수 있음 | 1의 결과 | 서버를 처음부터 ANSI 모드로 띄워 Silver의 `SET`과 같은 값으로 맞춤 |
| 3. 한 줄 SQL이 끝나지 않고 멈춤 | 끝에 `;`가 없으면 beeline이 다음 줄을 표준 입력에서 기다림 | `sql.sh -e`가 `;`를 붙이고 표준 입력을 막음 |
| 4. Bronze 작업을 보낼 때 카탈로그 비밀번호가 화면에 찍힘 | `sql-client -f`가 보낸 문장을 되찍음 | `submit-bronze.sh`가 그 줄을 가림 |
| 5. 레이크를 지우고 다시 만들면 Bronze가 빔 | Flink의 Kafka 읽은 위치가 Kafka에 남음 | 4절처럼 consumer group을 처음으로 되돌림 |

## 7. 하지 않은 것

- **손익 SQL**([`sql/pnl/`](../../sql/pnl/README.md))은 Gold에 넣지 않았습니다. 재귀 CTE를 쓰는데, 이 환경의 Spark 3.5.6에 `WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT * FROM t`를 보내 보니 `PARSE_SYNTAX_ERROR`로 거절됐습니다(11/12 확인).
- **뉴스와 관심도**(`news_items`, `attention_daily` 등)는 원천 테이블만 있고 비어 있습니다. Bronze 토픽 패턴에도 없습니다.
- **시세 정정**: 시세는 모두 첫 적재(`c`)라, 제공처가 지난 값을 고치는 `u`는 아직 돌려 보지 않았습니다.
- **정정과 철회 시나리오**(가이드 05 12절 ④의 6a, 6b)는 원천을 바꾸는 일이라 돌리지 않았습니다.
- **Silver는 매번 Bronze 전체를 읽습니다.** 증분 읽기는 6회차 주제입니다.
- **세 테이블의 반영 시점**: 체결, 주문, 잔고는 원천에서 한 트랜잭션이지만 Bronze와 Silver는 테이블마다 따로 commit합니다(가이드 04 12-7). 이번에는 적재가 끝난 뒤 Silver와 dbt를 돌려 어긋남이 없었습니다.

## 원본

| 무엇 | 파일 |
|---|---|
| 구성 | [`docker-compose.yml`](../../docker-compose.yml), [`infra/flink/`](../../infra/flink/Dockerfile), [`infra/spark/`](../../infra/spark/Dockerfile), [`infra/dbt/`](../../infra/dbt/Dockerfile) |
| Bronze 작업 | [`infra/flink/bronze.sql`](../../infra/flink/bronze.sql) |
| Silver | [`sql/silver/`](../../sql/silver/), 검증 [`sql/silver/checks/`](../../sql/silver/checks/expected.sql) |
| Gold | [`dbt/`](../../dbt/dbt_project.yml) |
