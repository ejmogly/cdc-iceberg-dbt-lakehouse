# 0001 로컬 실행 환경

- 날짜: 2026-11-04
- 상태: 결정

## 맥락

노트북 한 대(Apple Silicon)에서 PostgreSQL부터 Airflow까지 띄워야 합니다.
다른 사람이 같은 명령으로 따라 실행할 수 있어야 하고, 비밀값이 저장소에 남으면 안 됩니다.

## 결정

1. 컨테이너 엔진은 Colima를 씁니다. VM은 CPU 6, 메모리 12GB, 디스크 60GB입니다.
2. `docker-compose.yml` 하나에 profiles를 나눕니다. 기본 profile은 2회차까지 쓰는 postgres, kafka, connect, kafka-ui입니다. 뒤 회차의 서비스는 그 회차에 더합니다.
3. Kafka는 ZooKeeper 없는 KRaft 단일 노드로 띄웁니다.
4. 포트는 `127.0.0.1`에만 엽니다.
5. 비밀값은 `.env`에만 둡니다. compose는 `${VAR:?}`로 값이 없으면 멈춥니다. 커넥터 설정에는 `${env:VAR}`를 쓰고, Kafka Connect의 `EnvVarConfigProvider`가 컨테이너 환경 변수로 바꿉니다.
6. 이미지 태그는 패치 버전까지 고정합니다.
7. Kafka Connect는 quay.io의 `debezium/connect` 이미지 대신, Docker Hub의 `apache/kafka:4.3.2`에 Maven Central의 Debezium PostgreSQL 플러그인(`3.6.3.Final`, sha1 확인)을 넣어 직접 빌드합니다.
   - HTTPS를 검사하는 프록시가 있는 네트워크에서는 quay.io에서 이미지를 받지 못할 수 있습니다.
   - 이미지 출처가 Docker Hub와 Maven Central 두 곳으로 줄고, 브로커와 Connect의 Kafka 버전이 같아집니다.
   - 워커 설정(`infra/connect/connect-distributed.properties`)을 직접 다루게 됩니다.
   - 뒤 회차의 이미지와 jar도 되도록 Docker Hub와 Maven Central에서 받습니다.

## 검토한 다른 방법

| 방법 | 고르지 않은 이유 |
|---|---|
| Docker Desktop | 쓰는 곳에 따라 상용 구독 조건이 걸릴 수 있음 |
| OrbStack | 쓰는 곳에 따라 유료 |
| 클라우드 VM | 비용이 들고, 접속 정보가 공개 저장소에 섞일 위험이 있음 |
| 서비스마다 compose 파일 분리 | 네트워크와 볼륨 이름을 맞추는 일이 늘어남 |

## 결과

- 쉬는 기간에는 `colima stop`으로 VM까지 내립니다.
- 뒤 회차에서 메모리가 모자라면 profile을 나눠 띄우거나 VM 메모리를 늘리고 이 문서를 고칩니다.
