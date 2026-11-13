#!/usr/bin/env bash
# Bronze에서 Silver 현재 상태 다섯 테이블을 만듭니다(없으면 만들고, 있으면 MERGE).
# 테이블마다 1_incoming.sql(검사와 최신 후보)과 2_merge.sql(MERGE)을 한 세션에서 돌립니다.
# 같은 입력으로 여러 번 돌려도 업무 값은 같습니다(docs/decisions/0010).
#   infra/spark/silver.sh                  # 다섯 테이블 모두
#   infra/spark/silver.sh holdings orders  # 고른 테이블만
set -euo pipefail
cd "$(dirname "$0")/../.."

tables=("$@")
[ ${#tables[@]} -gt 0 ] || tables=(orders executions holdings daily_prices fx_rates)

for t in "${tables[@]}"; do
  infra/spark/sql.sh "sql/silver/$t/1_incoming.sql" "sql/silver/$t/2_merge.sql"
done
