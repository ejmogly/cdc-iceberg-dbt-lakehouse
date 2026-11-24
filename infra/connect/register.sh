#!/usr/bin/env bash
# Kafka Connect에 커넥터를 등록하거나 설정을 바꿉니다(PUT이라 여러 번 실행해도 같음).
#   infra/connect/register.sh                    # source-postgres.json
#   infra/connect/register.sh 다른설정.json
# 등록한 뒤 커넥터가 RUNNING이 될 때까지 30초 안에서 기다립니다. 등록 직후에는 상태가 아직 없어 404가 납니다
# (2026-11-24 처음부터 다시 올려 보기에서 찾음).
set -euo pipefail

cd "$(dirname "$0")"
file="${1:-source-postgres.json}"
# 포트는 셸 환경 변수, 없으면 저장소의 .env, 그것도 없으면 기본값(docker compose와 같은 값을 보게)
if [ -z "${CONNECT_PORT:-}" ] && [ -f ../../.env ]; then
  # 따옴표, 줄 끝 주석, CRLF(Windows에서 고친 파일)를 지우고 읽음
  CONNECT_PORT=$(sed -n 's/^CONNECT_PORT=//p' ../../.env | tail -1 | tr -d '\r"' | tr -d "'" | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//')
fi
port="${CONNECT_PORT:-18083}"

name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$file")
config=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["config"]))' "$file")

curl -fsS -X PUT -H 'Content-Type: application/json' \
  --data "$config" \
  "http://localhost:${port}/connectors/${name}/config"
echo
sleep 2  # 설정을 바꾸면 커넥터가 다시 시작하므로, 바뀌기 전의 RUNNING을 읽지 않게 잠시 기다림

state=""
for _ in $(seq 1 30); do
  status=$(curl -fsS "http://localhost:${port}/connectors/${name}/status" 2>/dev/null || true)
  state=$(printf '%s' "$status" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d["connector"]["state"] if all(t["state"] == "RUNNING" for t in d["tasks"]) and d["tasks"] else "")
except Exception:
    print("")' )
  [ "$state" = "RUNNING" ] && break
  sleep 1
done
echo "${status:-상태를 받지 못함}"
if [ "$state" != "RUNNING" ]; then
  echo "커넥터 ${name}이(가) 30초 안에 RUNNING이 되지 않았습니다. 위 상태의 오류를 보세요" >&2
  exit 1
fi
echo "RUNNING"
