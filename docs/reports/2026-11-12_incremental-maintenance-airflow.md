# 증분 Gold, Iceberg 유지보수, Airflow DAG 첫 실행 (2026-11-12)

[첫 실행 보고서](2026-11-12_lake-bronze-silver-gold.md)의 환경 위에서 6 ~ 8회차 내용을 하나씩 돌린 기록입니다.
세 가지 모두 최소 하나씩입니다. 증분은 Gold 한 테이블, 유지보수는 Silver 한 테이블 한 주기, Airflow는 DAG 하나입니다.

## 요약

| 회차 | 확인 | 결과 |
|---|---|---|
| 6 증분 | 일별 종목별 체결 대금을 바뀐 (종목, 거래일)만 다시 계산, 정답지(dbt 전체 재계산)와 모든 칸 비교 | 여섯 경우 모두 `증분에만 0 / 정답지에만 0`(1절) |
| 6 증분 | 지난 체결의 가격 정정, 날짜 정정(옛 거래일 0건) | 영향 키가 각각 1개, 2개. 옛 거래일 행은 지워짐 |
| 6 증분 | Gold를 고친 뒤 progress 저장 전에 멈추고 다시 돌림 | 저장한 plan을 그대로 써서 0 / 0 |
| 6, 7 | progress 스냅샷까지 만료한 뒤 | 증분이 이유를 찍고 전체 재계산으로 돌아가 0 / 0, 다음 실행부터 다시 변경만 읽음 |
| 7 유지보수 | Silver `executions`에 압축, 만료, 고아 정리, manifest 합치기 | 행 수와 합계 그대로. 스냅샷 10 → 3, 저장소의 데이터 파일 10 → 2(2절) |
| 7 빈 commit | Bronze 다섯 테이블, Flink 작업 19분 49초 | 테이블마다 빈 commit 10 ~ 11개(약 108초에 하나) |
| 8 Airflow | DAG `lakehouse_refresh`: Silver MERGE → `dbt build` → 증분 Gold | 정기 실행 1번, 수동 실행 1번 성공. Spark를 멈춰 일부러 실패시킨 실행은 1분 뒤 재시도로 성공(3절) |

## 측정 조건

| 항목 | 값 |
|---|---|
| 기기, VM | Apple M3 Pro 36GB, Colima VM CPU 6, 메모리 12GiB. Airflow까지 다 띄웠을 때 컨테이너 메모리 합 약 5.7GiB(Airflow 1.4GiB) |
| 버전 | [0002](../decisions/0002-lake-versions.md). 이번에 Airflow `apache/airflow:2.10.5-python3.11`을 더함 |
| Flink | 병렬도 1, checkpoint 10초. Bronze 테이블 다섯 개(4회차 네 개와 `fx_rates`) |
| Iceberg | 모든 테이블 format-version 2, copy-on-write, 파티션 없음 |
| 데이터 | 합성 주문, 체결, 잔고(시나리오 01 ~ 09), 공개 시세 4,736행, 환율 245행 |
| 시각 | 표의 시각은 UTC(서울은 9시간 더함) |

## 실행 명령

```bash
docker compose --profile flink \
  --profile spark up -d
infra/flink/submit-bronze.sh
infra/spark/silver.sh
docker compose --profile spark \
  run --rm dbt build
docker compose --profile spark run --rm \
  --entrypoint python dbt \
  /workspace/sql/gold_incremental/run.py \
  --compare
```

| 파일 | 하는 일 |
|---|---|
| [`sql/gold_incremental/run.py`](../../sql/gold_incremental/run.py) | 증분 한 번(구간 고정 → plan 저장 → Gold MERGE → progress 저장). `--compare`는 정답지와 비교, `--full`은 전체 재계산, `--fail-after-merge`는 시험용 멈춤 |
| [`sql/maintenance/maintain.py`](../../sql/maintenance/maintain.py) | 테이블 하나의 유지보수 한 주기와 단계별 숫자 |
| [`infra/spark/run_sql.py`](../../infra/spark/run_sql.py) | beeline 없이 SQL 파일을 한 세션에서 돌림(Airflow가 씀) |
| [`airflow/dags/lakehouse_refresh.py`](../../airflow/dags/lakehouse_refresh.py) | 30분마다 Silver, dbt, 증분 |
| [`infra/postgres/scenario/07 ~ 09`](../../infra/postgres/scenario/README.md) | 증분 실험용 원천 변경 |

## 1. 증분 Gold (6회차)

### 설계

가이드 06의 7 ~ 12절, 14절을 이 저장소의 일별 종목별 체결 대금(`mart_daily_symbol_trading`)에 옮겼습니다.

| 물을 것 | 이 저장소의 답 |
|---|---|
| 변경을 어디서 읽나 | Silver 현재 상태 `executions`, `orders`. `create_changelog_view`로 두 스냅샷 사이의 바뀐 행(carry-over 제외) |
| 진행 위치 | `lake.gold.mart_daily_symbol_trading_inc_progress`(입력마다 스냅샷). 마지막에 `INSERT OVERWRITE` 한 번으로 저장 |
| 영향 키 | (`symbol`, `trade_date`). 체결 변경은 주문을 거쳐 종목을, 체결 시각과 시장 시간대로 거래일을 얻음. 변경 뷰의 모든 행(바뀌기 전 행 포함)을 써서 옛 거래일도 잡음 |
| 다시 계산 | 영향 키마다 끝 스냅샷으로 처음부터 다시 합산(차액 아님). 조건과 날짜 규칙은 dbt `fct_execution`과 같음 |
| 지우기 | 영향 키를 왼쪽에 둔 `MERGE`. 체결이 0건이 된 키는 `DELETE` |
| 중간 실패 | `..._inc_plan`에 range_id와 영향 키를 먼저 저장, 같은 구간이면 재사용 |
| 비상 경로 | progress가 없거나, progress 스냅샷이 만료됐거나, `--full`이면 모든 키를 다시 계산 |
| 정답지 | dbt `table` 모델 `lake.gold.mart_daily_symbol_trading`. 증분 결과는 `..._inc` |

### 시나리오와 결과

새 시나리오 07 ~ 09는 계좌 `A-0002`의 미국 종목 `SPY` 체결입니다(01 ~ 06은 그대로). 단계마다 원천 변경 → Bronze → Silver MERGE → `dbt build`(정답지) → 증분 `--compare` 순서로 돌렸습니다. `dbt build`는 매번 55개 통과였습니다.

| 단계 | 원천 변경 | 증분 모드 | 영향 키 | 바뀐 Gold 행(SPY) | 비교 |
|---|---|---|---|---|---|
| i0 첫 실행 | 없음(01 ~ 06의 결과) | full(progress 없음) | (005930, 11-11) | | 0 / 0 |
| 07 지난 두 거래일 매수 | 주문 2001, 2002, 체결 6001(11-09, 10주 x 750.00), 6002(11-10, 5주 x 748.50), 잔고 15주 | changes | (SPY, 11-09), (SPY, 11-10) | 11-09: 1건 10주 7,500.00 / 11-10: 1건 5주 3,742.50 | 0 / 0 |
| 08 가격 정정 | 체결 6001 750.00 → 749.80, 잔고 평균 단가 | changes | (SPY, 11-09) | 11-09: 7,498.00 | 0 / 0 |
| 09 날짜 정정(첫 시도) | 주문 2002와 체결 6002를 11-10 → 11-09 | changes | (SPY, 11-09), (SPY, 11-10) | 11-09: 2건 15주 11,240.50, 11-10 행 지워짐 | Gold를 고친 뒤 일부러 멈춤 |
| 09 재시도 | 없음(같은 구간) | 저장한 plan 재사용 | 같음 | 같음 | 0 / 0 |
| 새 변경 없음 | 없음 | 스냅샷이 그대로라 할 일 없음 | | | 0 / 0 |
| Silver MERGE만 다시 | 없음(스냅샷만 새로 생김) | changes | 0개(carry-over만 있음) | | 0 / 0 |

- 08에서 주문 테이블도 MERGE로 새 스냅샷이 생겼지만, 변경 뷰가 carry-over를 걸러 바뀐 행이 0이었습니다. 그래서 영향 키가 늘지 않았습니다.
- 09의 멈춘 순간에는 증분 Gold가 이미 11-10 행을 지우고 11-09를 2건으로 고쳤고, progress는 08의 스냅샷 그대로였습니다. 다시 돌리자 같은 range_id라 plan을 새로 짜지 않고 저장한 영향 키 두 개를 썼습니다(가이드 06 12절의 정상 모드).
- 09의 옛 거래일 11-10은 변경 뷰의 바뀌기 전 행(`UPDATE_BEFORE`)의 체결 시각에서 나왔습니다(가이드 06 9절 ③).
- 시나리오 07 ~ 09 뒤 Kafka 끝 위치는 orders 9, executions 6, holdings 6이고, Bronze는 9, 6, 5입니다(tombstone 1개 제외).

### progress 스냅샷을 만료한 경우(가이드 07 8절)

Silver `orders`에 새 스냅샷을 하나 만든 뒤 `expire_snapshots(older_than => 지금, retain_last => 1)`로 스냅샷 17개를 1개로 줄였습니다. progress가 가리키던 스냅샷도 사라졌습니다.

| 시도 | 일어난 일 |
|---|---|
| 고치기 전 실행기 | 변경 뷰를 만드는 `CALL`은 성공했지만, 그 뷰를 읽는 순간 지워진 데이터 파일(`NotFoundException`)에서 실패. plan 저장은 commit 한 번이라 반쯤 저장된 것은 없음 |
| 고친 실행기 | 시작 스냅샷이 남아 있는지 먼저 확인해 "progress 스냅샷이 만료돼 없습니다"를 찍고 full 모드로 영향 키 2개를 다시 계산, 0 / 0 |
| 그다음 실행 | 새 progress부터 다시 changes 모드, 0 / 0 |

## 2. Iceberg 유지보수 (7회차)

대상은 증분이 읽는 Silver `executions`입니다. Flink가 쓰지 않는 테이블이라 Flink 작업을 멈추지 않았습니다(가이드 07 10절).
만료 기준은 「증분 progress 스냅샷의 커밋 시각」과 「지금 - 보존 기간」 가운데 이른 쪽입니다(가이드 07 9절). 실습이라 보존 기간을 0분으로 줘서, progress 스냅샷의 커밋 시각(05:53:55)이 기준이 됐습니다. 고아 정리 기준은 3일 전입니다.

| 단계 | 행 수 | 합계(수량 x 가격) | 현재 데이터 파일 | 모든 스냅샷의 데이터 파일 | 스냅샷 | 현재 manifest | 모든 manifest | metadata 이력 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 정리 전 | 4 | 569,640.5 | 1 | 10 | 10 | 2 | 19 | 11 |
| 1 `rewrite_data_files` | 4 | 569,640.5 | 1 | 10 | 10 | 2 | 19 | 11 |
| 2 `expire_snapshots` | 4 | 569,640.5 | 1 | 2 | 2 | 2 | 4 | 12 |
| 3 `remove_orphan_files` | 4 | 569,640.5 | 1 | 2 | 2 | 2 | 4 | 12 |
| 4 `rewrite_manifests` | 4 | 569,640.5 | 1 | 2 | 3 | 1 | 5 | 13 |

저장소에 실제로 있는 파일(`mc ls`)은 데이터 10 → 2개, 메타데이터 40 → 21개였습니다.

- **압축이 한 일이 없음**: 현재 스냅샷의 데이터 파일이 이미 1개라 합칠 것이 없었습니다(`min-input-files` 2). copy-on-write `MERGE`가 매번 파일을 통째로 다시 써 온 결과입니다.
- **만료가 공간을 비움**: 옛 스냅샷 8개와 그것만 가리키던 데이터 파일 8개, manifest가 지워졌습니다. 남은 2개는 progress 스냅샷과 그 뒤의 스냅샷입니다.
- **고아 0개**: 모든 파일이 오늘 만든 것이라 3일 기준에 걸리는 것이 없었습니다(`dry_run` 먼저). Iceberg는 24시간보다 최근 기준을 받지 않습니다.
- **manifest 합치기가 스냅샷을 하나 더함**: 2 → 3. 다음 주기의 만료가 치웁니다.
- 정리 뒤 증분은 progress 스냅샷부터 이어 읽어 0 / 0이었습니다.

**Bronze의 빈 commit**(첫 보고서 1절에서 본 것): 05:46:21에 시작한 Flink 작업이 06:06:10까지 checkpoint 119번을 commit했습니다. 그동안 Bronze 테이블마다 행 없는 `append` 스냅샷이 쌓였습니다.

| Bronze | 스냅샷 | 빈 commit | 데이터가 있는 commit |
|---|---:|---:|---:|
| `orders` | 13 | 11 | 2 |
| `executions` | 13 | 10 | 3 |
| `holdings` | 12 | 10 | 2 |
| `daily_prices` | 11 | 11 | 0 |
| `fx_rates` | 11 | 11 | 0 |

빈 checkpoint 10번마다 한 번꼴(약 108초에 하나)이라, 10초 checkpoint로 하루를 두면 테이블마다 약 800개입니다. Bronze 정리는 Flink 작업을 멈춘 뒤 같은 실행기로 돌리면 되지만 이번에는 하지 않았습니다.

## 3. Airflow DAG (8회차)

[`airflow/dags/lakehouse_refresh.py`](../../airflow/dags/lakehouse_refresh.py)는 태스크 셋을 차례로 잇습니다. 강사 저장소의 DAG는 참고만 했습니다(그 저장소에는 라이선스 파일이 없음).

| 설정 | 값 | 이유 |
|---|---|---|
| 태스크 | `silver_merge` → `dbt_build` → `gold_incremental` | Silver를 고친 뒤 정답지와 증분을 만듦. 증분 태스크는 정답지와 0 / 0이 아니면 실패 |
| 주기 | `*/30 * * * *`, 처음에는 멈춘 상태 | 가이드 06 13절의 30분 설계 |
| 재시도 | 2번, 1분 간격 | 실습에서 기다리기 좋게 짧게. 운영이라면 3분 이상 |
| 동시 실행 | `max_active_runs=1` | 두 실행이 같은 progress를 읽고 덮어쓰지 않게 |
| `catchup` | 끔 | 증분은 Airflow 날짜 구간이 아니라 progress부터 읽음 |
| dbt | 이미지 안의 따로 된 가상환경(dbt 컨테이너와 같은 lock), dbt 폴더는 읽기 전용 | Airflow 의존성과 섞지 않음 |

| 실행 | 시작 ~ 끝(UTC) | 결과 | 태스크별 |
|---|---|---|---|
| `scheduled__2026-11-12T05:30:00` | 06:02:30 ~ 06:02:59 | 성공 | 멈춤을 풀자 `catchup=False`라 가장 최근 구간 하나가 바로 돔 |
| `manual__first_run`(수동) | 06:03:00 ~ 06:03:28 | 성공 | Silver 4.7초, dbt 14.8초(55개 통과), 증분 2.1초(0 / 0) |
| `manual__retry_demo`(수동) | 06:03:54 ~ 06:05:38 | 성공 | 아래 |

재시도 시험은 Spark 컨테이너를 멈춘 채 실행을 넣고, 첫 시도가 실패한 뒤 Spark를 다시 올렸습니다.

| 시각 | 일어난 일(태스크 로그) |
|---|---|
| 06:03:57 | `silver_merge` 1번째 시도: `failed to resolve sockaddr for spark:10000`, 종료 코드 1, `UP_FOR_RETRY` |
| 06:04:09 | Spark Thrift server 다시 뜸(healthy) |
| 06:04:59 ~ 06:05:16 | 2번째 시도: 다섯 테이블 `1_incoming.sql` 문장 7개, `2_merge.sql` 문장 1개씩 돌고 성공 |
| 06:05:18 ~ 06:05:38 | `dbt_build` 55개 통과, `gold_incremental` 영향 키 0개, 0 / 0 |

로그는 컨테이너의 `/opt/airflow/logs/dag_id=lakehouse_refresh/run_id=manual__retry_demo/` 아래 시도별 파일에 있습니다(볼륨 `airflow-home`, 저장소에는 넣지 않음). 시험 뒤 DAG는 다시 멈춰 두었습니다.

## 4. 겪은 문제와 고친 것

| 문제 | 원인 | 고친 것 |
|---|---|---|
| 1. 원본 `.changes` 테이블을 세기만 해도 Spark 내부 오류(`unexpected number of columns`) | 이 판의 Spark와 Iceberg 조합 문제로 보임(가이드 06 7절에도 비슷한 기록) | 변경은 `create_changelog_view`로만 읽음 |
| 2. pyhive로 `CREATE` 같은 문장의 결과를 읽으려다 실패 | 결과가 없는 문장의 응답을 읽음 | 결과가 필요한 조회만 읽도록 나눔 |
| 3. progress 스냅샷이 만료되면 비상 경로로 가지 못함 | 변경 뷰를 만드는 `CALL`은 성공하고 읽을 때 실패 | 시작 스냅샷이 남았는지 먼저 확인, 읽다 실패해도 전체 재계산 |
| 4. `expire_snapshots(older_than => current_timestamp())`가 구문 오류 | Iceberg 프로시저 인자는 식이 아니라 값이어야 함 | 시각을 먼저 구해 `TIMESTAMP '...'`로 넣음(실행기는 처음부터 이렇게 씀) |

## 5. 하지 않은 것

- 증분 마트는 하나(일별 종목별 체결 대금)만 만들었습니다. 잔고 대사 마트, 시세 정정이 창 길이만큼 퍼지는 지표(가이드 06 14절 마지막 줄)는 하지 않았습니다.
- 가이드 06의 `--naive-dates`(plan 없이 다시 짜기) 비교는 하지 않았습니다. 이 마트는 키에 날짜가 들어 있어 바뀌기 전 행만으로도 옛 날짜를 잡습니다.
- 재시도 전에 새 변경이 들어와 구간이 넓어지는 경우(가이드 06 12절 끝)는 돌리지 않았습니다.
- dbt의 `incremental` 모델은 쓰지 않았습니다. 0건이 된 키를 지우는 분기가 기본 매크로에 없어서입니다(가이드 06 10절).
- Bronze 테이블 정리, `write.metadata.delete-after-commit.enabled` 같은 테이블 속성은 하지 않았습니다.
- Airflow는 2.10.5(수업 환경과 같은 판)입니다. 3.x로 올리는 것은 8회차 자료를 본 뒤 정합니다.

## 원본

| 무엇 | 파일 |
|---|---|
| 증분 | [`sql/gold_incremental/run.py`](../../sql/gold_incremental/run.py), 시나리오 [`infra/postgres/scenario/`](../../infra/postgres/scenario/README.md) 07 ~ 09 |
| 유지보수 | [`sql/maintenance/maintain.py`](../../sql/maintenance/maintain.py) |
| Airflow | [`airflow/dags/lakehouse_refresh.py`](../../airflow/dags/lakehouse_refresh.py), [`infra/airflow/Dockerfile`](../../infra/airflow/Dockerfile), [`infra/spark/run_sql.py`](../../infra/spark/run_sql.py) |
