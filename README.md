# CDC, Iceberg, dbt 실시간 레이크하우스

PostgreSQL의 변경을 Debezium과 Kafka로 받아 Flink로 Apache Iceberg에 쌓고, Spark SQL과 dbt로 분석 마트까지 갱신하는 개인 프로젝트입니다.
빠르게 가져오는 것보다 **정정, 삭제, 재처리 뒤에도 지표가 맞는지**를 측정과 검증 기록으로 보이는 것이 목표입니다.

> 상태: 진행 중, 0주차(실행 환경과 저장소 뼈대). 계획과 진행 기록은 [docs/PLAN.md](docs/PLAN.md)에 있습니다.
> 처음이라면 입문 가이드 [00. 프로젝트 한눈에 보기](docs/guide/00-overview.md)부터 읽으세요.

## 아키텍처

```text
PostgreSQL ──(Debezium, WAL)──▶ Kafka ──(Flink)──▶ Iceberg Bronze   변경 이력
                                                      │
                                                      ▼ Spark SQL MERGE
                                                    Iceberg Silver   키별 현재 상태
                                                      │
                                                      ▼ dbt (dbt-spark)
                                                    Iceberg Gold     분석 마트
```

- 저장소는 MinIO(S3 호환), 카탈로그는 Iceberg REST 카탈로그를 씁니다(3회차에 확정).
- Airflow는 변환과 테스트 작업의 순서와 재시도를 맡습니다(8회차).

## 확인할 것

| 질문 | 어떻게 보이나 |
|---|---|
| 원천의 UPDATE, DELETE가 최종 집계에 빠짐없이 반영되는가 | 원천과 마트의 건수, 합계 대조 |
| 스트리밍 처리가 유입 속도를 따라가는가 | 원천 변경 → Iceberg 조회 가능 → 마트 갱신, 구간별 지연 |
| 작업이 중단됐다 다시 돌아도 중복, 누락이 없는가 | 재전달, 중단, 백필 뒤 결과 대조 |

숫자는 모두 측정 조건과 함께 [docs/reports/](docs/reports/)에 남깁니다.

## 실행

준비물은 Docker 엔진(이 저장소는 Colima로 확인)과 컨테이너용 메모리 8GB 이상입니다.

```bash
cp .env.example .env        # 비밀번호를 바꿉니다
docker compose up -d        # 기본 profile: postgres, kafka, connect, kafka-ui
docker compose ps
```

| 서비스 | 주소 |
|---|---|
| Kafka UI | http://localhost:18080 |
| Kafka Connect REST | http://localhost:18083 |
| PostgreSQL | `localhost:5432` |
| Kafka(호스트에서) | `localhost:29092` |

정리할 때는 `docker compose down`, 데이터까지 지우려면 `docker compose down -v`입니다.

macOS에서 Colima를 쓸 때:

```bash
brew install colima docker docker-compose
colima start --cpu 6 --memory 12 --disk 60
```

## 폴더

```text
.
├── docker-compose.yml   단계별 profile로 나눈 로컬 실행 구성
├── .env.example         비밀값 이름과 자리표시 값 (.env는 git 제외)
├── infra/
│   ├── postgres/init/   최초 기동 때 스키마, 복제 계정, publication 생성
│   └── connect/         Connect 이미지, 워커 설정, Debezium 커넥터 설정과 등록 스크립트
├── data/                데이터 출처와 라이선스 (원본 파일은 git 제외)
└── docs/
    ├── PLAN.md          주차 계획과 진행 기록
    ├── guide/           회차별 입문 가이드(개념, 따라 하기, 점검 질문)
    ├── design/          3회차 설계 산출물
    ├── decisions/       결정 기록
    └── reports/         지연, 복구, 성능 측정 보고서
```

Flink 작업, Spark SQL, dbt 프로젝트, Airflow DAG 폴더는 해당 회차에 더합니다.

## 버전

| 구성 | 이미지 |
|---|---|
| PostgreSQL | `postgres:16.15` |
| Kafka (KRaft) | `apache/kafka:4.3.2` |
| Kafka Connect + Debezium | `apache/kafka:4.3.2`에 Debezium PostgreSQL 커넥터 `3.6.3.Final`을 넣어 빌드(`infra/connect/Dockerfile`) |
| Kafka UI | `kafbat/kafka-ui:v1.5.0` |

처음 `docker compose up` 할 때 Connect 이미지를 빌드합니다. 이미지는 Docker Hub에서, 플러그인은 Maven Central에서 받습니다.

## 참고

- 메타코드M 「실시간 데이터 수집부터 분석 마트까지 — CDC, Iceberg, dbt」 스터디 커리큘럼을 바탕으로 혼자 진행합니다.
- [Debezium PostgreSQL Connector](https://debezium.io/documentation/reference/stable/connectors/postgresql.html)
- [Apache Iceberg](https://iceberg.apache.org/docs/latest/)
- [Iceberg Flink Writes](https://iceberg.apache.org/docs/latest/flink-writes/)
- [dbt 소개](https://docs.getdbt.com/docs/introduction)
