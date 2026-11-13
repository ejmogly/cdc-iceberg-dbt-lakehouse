#!/usr/bin/env bash
# Spark Thrift server에 SQL을 보냅니다(spark 컨테이너 안의 beeline).
# 파일을 여러 개 주면 이어 붙여 한 세션에서 차례로 돌립니다. 앞 파일이 만든 임시 뷰를 뒤 파일이 씁니다.
# 첫 오류에서 멈추고 0이 아닌 값으로 끝납니다.
#   infra/spark/sql.sh sql/silver/orders/1_incoming.sql sql/silver/orders/2_merge.sql
#   infra/spark/sql.sh -e "SELECT count(*) FROM lake.bronze.orders"
# 결과를 표 대신 탭으로 나눈 값으로 받으려면 SQL_FORMAT=tsv2를 앞에 붙입니다.
set -euo pipefail
cd "$(dirname "$0")/../.."

beeline=(/opt/spark/bin/beeline
  -u 'jdbc:hive2://localhost:10000/default;auth=noSasl' -n cdclake
  --silent=true --outputformat="${SQL_FORMAT:-table}")

if [ "${1:-}" = "-e" ]; then
  # 끝에 ;가 없으면 beeline이 다음 줄을 표준 입력에서 기다리며 멈춥니다. ;를 붙이고 입력을 막습니다.
  sql="${2%"${2##*[![:space:]]}"}"
  case "$sql" in *\;) ;; *) sql="$sql;" ;; esac
  docker compose exec -T spark "${beeline[@]}" -e "$sql" < /dev/null
  exit
fi

for f in "$@"; do
  [ -f "$f" ] || { echo "없는 파일: $f" >&2; exit 2; }
done
echo "== $*" >&2
cat "$@" | docker compose exec -T spark bash -c '
  set -euo pipefail
  script=$(mktemp /tmp/run-XXXXXX.sql)
  trap "rm -f $script" EXIT
  cat > "$script"
  "$@" -f "$script"
' _ "${beeline[@]}"
