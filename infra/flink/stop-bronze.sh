#!/usr/bin/env bash
# 돌고 있는 Bronze 적재 작업을 savepoint를 남기고 멈춥니다. 마지막 줄에 savepoint 경로를 찍습니다.
#   infra/flink/stop-bronze.sh
#   infra/flink/submit-bronze.sh <찍힌 경로>     # 멈춘 자리에서 다시 시작
set -euo pipefail
cd "$(dirname "$0")/../.."

api() { docker compose exec -T jobmanager wget -qO- "$@"; }

jid=$(api http://localhost:8081/jobs/overview \
  | python3 -c 'import json,sys; print(next((j["jid"] for j in json.load(sys.stdin)["jobs"] if j["name"] == "bronze-brokerage" and j["state"] == "RUNNING"), ""))')
if [ -z "$jid" ]; then
  echo "돌고 있는 bronze-brokerage 작업이 없습니다." >&2
  exit 1
fi

trigger=$(api --header 'Content-Type: application/json' \
  --post-data '{"drain": false, "targetDirectory": "file:///opt/flink/state/savepoints"}' \
  "http://localhost:8081/jobs/$jid/stop" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["request-id"])')

for _ in $(seq 1 60); do
  result=$(api "http://localhost:8081/jobs/$jid/savepoints/$trigger")
  status=$(echo "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["id"])')
  if [ "$status" = "COMPLETED" ]; then
    echo "$result" | python3 -c '
import json, sys
op = json.load(sys.stdin)["operation"]
if "location" not in op:
    sys.exit("savepoint 실패: " + json.dumps(op)[:500])
print(op["location"])'
    exit 0
  fi
  sleep 2
done
echo "savepoint가 2분 안에 끝나지 않았습니다." >&2
exit 1
