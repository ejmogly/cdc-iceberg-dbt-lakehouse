#!/usr/bin/env bash
# Kafka Connect에 커넥터를 등록하거나 설정을 바꿉니다(PUT이라 여러 번 실행해도 같음).
#   infra/connect/register.sh                    # source-postgres.json
#   infra/connect/register.sh 다른설정.json
set -euo pipefail

cd "$(dirname "$0")"
file="${1:-source-postgres.json}"
port="${CONNECT_PORT:-18083}"

name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$file")
config=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["config"]))' "$file")

curl -fsS -X PUT -H 'Content-Type: application/json' \
  --data "$config" \
  "http://localhost:${port}/connectors/${name}/config"
echo
curl -fsS "http://localhost:${port}/connectors/${name}/status"
echo
