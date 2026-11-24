# 0002 Flink, Iceberg, 카탈로그, Spark, S3 호환 저장소, dbt 버전 조합

- 날짜: 2026-11-12
- 상태: 결정

## 맥락

Bronze(Flink가 Kafka를 읽어 Iceberg에 쌓음), Silver(Spark SQL `MERGE`), Gold(dbt)가 **같은 Iceberg 테이블을 같은 카탈로그로** 읽고 써야 합니다.
엔진마다 Iceberg 런타임 jar가 따로 있고, Flink와 Spark의 판마다 맞는 jar가 정해져 있어 하나를 올리면 다른 것도 따라 움직입니다.
11/13까지 네 원천 테이블의 Bronze, Silver, Gold를 실제로 돌려야 해서, 처음 맞춰 보는 조합보다 이미 한 번 끝까지 돌아간 조합이 낫습니다.
일부 네트워크에서 quay.io와 apache.org 내려받기가 막혀, 이미지는 Docker Hub, jar는 Maven Central, 파이썬 패키지는 PyPI에서 받습니다([0001](0001-local-runtime.md) 7번).

## 결정

스터디 공통 예제 환경에서 3 ~ 5회차 실습이 끝까지 돌아간 조합을 그대로 씁니다. 공통 예제 저장소에는 라이선스 파일이 없어 **파일은 옮기지 않고**, 버전 조합만 참고해 이 저장소의 Dockerfile, compose, SQL을 새로 썼습니다.

| 부품 | 버전 | 받는 곳 |
|---|---|---|
| Flink | `flink:1.20.4-scala_2.12-java17` | Docker Hub |
| Flink Kafka 커넥터 | `flink-sql-connector-kafka` 3.4.0-1.20 | Maven Central |
| Iceberg(Flink) | `iceberg-flink-runtime-1.20` 1.10.0 | Maven Central |
| Hadoop 클래스(Flink의 Iceberg가 필요로 함) | `flink-shaded-hadoop-2-uber` 2.8.3-10.0 | Maven Central |
| Spark | `apache/spark:3.5.6-java17` | Docker Hub |
| Iceberg(Spark) | `iceberg-spark-runtime-3.5_2.12` 1.10.0 | Maven Central |
| S3 접근(두 엔진 공통) | `iceberg-aws-bundle` 1.10.0 | Maven Central |
| 카탈로그 | Iceberg JDBC 카탈로그, `postgres:16.15`(원천 DB와 따로), JDBC 드라이버 `postgresql` 42.7.7 | Docker Hub, Maven Central |
| S3 호환 저장소 | `pgsty/silo` RELEASE.2026-10-18T00-00-00Z(MinIO 포크), 버킷 만들기는 `pgsty/mc` 같은 날짜. 둘 다 digest 고정 | Docker Hub |
| dbt | `dbt-core` 1.12.5, `dbt-spark[PyHive]` 1.9.3, `python:3.11-slim` | PyPI, Docker Hub |
| Airflow(11/12 더함) | `apache/airflow:2.10.5-python3.11` standalone. dbt는 이미지 안의 따로 된 가상환경에 같은 lock으로 | Docker Hub, PyPI |

1. **jar는 sha256을 Dockerfile에 고정**하고 받은 뒤 대조합니다([`infra/flink/Dockerfile`](../../infra/flink/Dockerfile), [`infra/spark/Dockerfile`](../../infra/spark/Dockerfile)). 고정한 값은 처음 받을 때 Maven Central의 `.sha1`과 맞춰 본 것입니다.
2. **dbt는 의존성 전체를 고정**합니다. 위 두 패키지를 설치한 뒤 `pip freeze`로 뽑은 [`infra/dbt/requirements.lock`](../../infra/dbt/requirements.lock)으로 이미지를 만듭니다.
3. **카탈로그는 JDBC 카탈로그**입니다. Flink와 Spark가 같은 PostgreSQL을 보고, 테이블마다 지금 가리키는 metadata 파일 경로 한 줄을 둡니다. 원천 DB(`postgres`)와 섞지 않으려고 `catalog-db`를 따로 띄웁니다.
4. **두 엔진 모두 UTC로** 시각을 다룹니다(Flink `table.local-time-zone`, Spark `spark.sql.session.timeZone`). 거래일은 Silver core에서 그 시장의 현지 날짜로 바꿉니다.
5. compose profile은 `lake`(카탈로그 DB, 저장소, 버킷 만들기), `flink`, `spark`(Thrift server), `dbt`, `airflow`(11/12 더함)입니다. dbt는 `docker compose --profile spark run --rm dbt`처럼 서비스를 바로 골라 부릅니다. lake 서비스는 세 profile에 모두 들어 있어 `flink`나 `spark`만 켜도 같이 뜹니다.

## 검토한 다른 방법

| 방법 | 고르지 않은 이유 |
|---|---|
| Iceberg 1.10.2(같은 줄의 최신 패치) | 고칠 것만 고친 판이라 위험은 작지만, 11/13까지는 끝까지 돌아간 1.10.0을 바꾸지 않음. 납기 뒤 첫 후보 |
| Iceberg 1.12.0, Flink 2.x, Spark 4.x | 엔진과 커넥터를 함께 올려야 하고 Kafka 커넥터 판도 바뀜. 이번 기한 안에 처음 맞춰 볼 시간이 없음 |
| Iceberg REST 카탈로그 | 카탈로그 서버 이미지가 하나 더 필요함. 회차 자료와 같은 JDBC 카탈로그가 비교하기 쉬움 |
| 원천 PostgreSQL에 카탈로그 DB를 함께 둠 | 컨테이너는 하나 줄지만, 원천을 다시 만들 때 레이크 목록까지 지워질 수 있음 |
| Docker Hub의 `minio/minio` | 2025년 가을 뒤로 새 판이 올라오지 않아 보안 고침을 받을 수 없음. 같은 명령과 API를 이어 가는 포크를 digest로 고정함 |
| `dbt-spark` 1.11.0 | 1.9.3이 dbt-core 1.12와 Iceberg 테이블에서 이미 돌아감. 올리면 lock을 다시 뽑음 |
| dbt를 호스트 가상환경에 설치 | `make check`가 쓰는 `.venv`와 섞임. 컨테이너면 `docker compose run` 한 줄로 같은 판을 씀 |
| Airflow 3.x | 수업 환경이 2.10.5라 비교하기 쉬운 쪽을 고름. 3.x는 연산자 위치와 인증이 바뀌어 8회차 자료를 본 뒤 정함 |
| Airflow에 dbt를 바로 설치 | Airflow와 dbt의 의존성 판이 부딪힐 수 있음. 가상환경을 나누면 dbt 컨테이너와 같은 lock을 그대로 씀 |

## 결과

- 11/12 Colima VM(CPU 6, 메모리 12GB)에서 기본 profile과 `flink`, `spark`를 모두 띄우고 Bronze, Silver, Gold를 돌렸습니다. 결과는 [보고서](../reports/2026-11-12_lake-bronze-silver-gold.md)에 있습니다.
- Spark Thrift server는 세션 하나를 모든 접속이 같이 씁니다(`singleSession=true`). 접속마다 세션을 만들면 세션마다 Iceberg JDBC 카탈로그가 카탈로그 DB 연결을 새로 열고 닫지 않아, 검증을 몇 번 돌리자 PostgreSQL 연결 한도를 넘겼습니다. 그래서 Silver와 dbt를 동시에 돌리지 않고, 서버를 ANSI 모드로 띄워 세션 설정을 하나로 맞춥니다.
- 손익 SQL([`sql/pnl/`](../../sql/pnl/README.md))은 재귀 CTE를 쓰는데, Spark 3.5.6은 `WITH RECURSIVE`를 구문 오류(`PARSE_SYNTAX_ERROR`)로 거절합니다(11/12 확인). 그래서 11/12 Gold에는 넣지 않았습니다. 11/18에 장부만 `aggregate` 접기로 바꾼 Spark 판으로 Gold 손익 마트(`pnl_position` 등, `dbt/models/pnl/`)를 더했습니다(0008 상태, [`sql/pnl/README.md`](../../sql/pnl/README.md)).
- 버전을 올릴 때는 이 문서에 새 줄을 더하고, jar sha256과 dbt lock을 다시 뽑고, 보고서의 검증을 같은 순서로 다시 돌립니다.
