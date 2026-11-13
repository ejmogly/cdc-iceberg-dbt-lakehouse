#!/usr/bin/env bash
# Bronze 적재 작업(bronze.sql)을 Flink에 보냅니다. 이미 돌고 있으면 그대로 둡니다.
#   infra/flink/submit-bronze.sh                      # Kafka group offset부터 이어서
#   infra/flink/submit-bronze.sh <savepoint 경로>      # stop-bronze.sh가 남긴 savepoint에서
# 비밀값은 jobmanager 컨테이너의 환경 변수(.env에서 옴)로 컨테이너 안에서만 채웁니다.
set -euo pipefail
cd "$(dirname "$0")/../.."

savepoint="${1:-}"

running=$(docker compose exec -T jobmanager wget -qO- http://localhost:8081/jobs/overview \
  | python3 -c 'import json,sys; print(sum(1 for j in json.load(sys.stdin)["jobs"] if j["name"] == "bronze-brokerage" and j["state"] in ("RUNNING", "CREATED", "RESTARTING")))')
if [ "$running" != "0" ]; then
  echo "bronze-brokerage 작업이 이미 돌고 있습니다."
  exit 0
fi

docker compose exec -T -e SAVEPOINT="$savepoint" jobmanager bash -c '
  set -euo pipefail
  out=$(mktemp /tmp/bronze-XXXXXX.sql)
  trap "rm -f $out" EXIT
  if [ -n "$SAVEPOINT" ]; then
    echo "SET '\''execution.savepoint.path'\'' = '\''$SAVEPOINT'\'';" > "$out"
  fi
  sed -e "s|@CATALOG_DB@|$CATALOG_DB|g" \
      -e "s|@CATALOG_USER@|$CATALOG_USER|g" \
      -e "s|@CATALOG_PASSWORD@|$CATALOG_PASSWORD|g" \
      -e "s|@S3_BUCKET@|$S3_BUCKET|g" \
      -e "s|@CHECKPOINT_INTERVAL@|$CHECKPOINT_INTERVAL|g" \
      /opt/flink/cdclake/bronze.sql >> "$out"
  # sql-client는 보낸 문장을 되찍으므로 비밀번호 줄은 가리고 보여 줍니다.
  /opt/flink/bin/sql-client.sh -f "$out" 2>&1 \
    | sed -E "s/(jdbc\.password'\'' = '\'')[^'\'']*/\1(가림)/"
'
