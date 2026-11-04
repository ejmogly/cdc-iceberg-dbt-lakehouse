#!/usr/bin/env bash
# 데이터 볼륨이 비어 있을 때(최초 기동) 한 번 실행됩니다.
# 원천 테이블을 둘 source 스키마, Debezium 복제 계정, publication을 만듭니다.
# 원천 테이블 DDL은 도메인을 정한 뒤 10_ 으로 시작하는 파일로 더합니다.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v cdc_user="$CDC_USER" -v cdc_password="$CDC_PASSWORD" <<'SQL'
CREATE SCHEMA IF NOT EXISTS source;

CREATE ROLE :"cdc_user" WITH LOGIN REPLICATION PASSWORD :'cdc_password';
GRANT USAGE ON SCHEMA source TO :"cdc_user";
GRANT SELECT ON ALL TABLES IN SCHEMA source TO :"cdc_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA source GRANT SELECT ON TABLES TO :"cdc_user";

-- source 스키마의 테이블은 나중에 만든 것까지 모두 이 publication에 들어갑니다.
CREATE PUBLICATION cdc_pub FOR TABLES IN SCHEMA source;
SQL
