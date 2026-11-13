#!/usr/bin/env bash
# Silver 검증 세 가지(docs/decisions/0010, 가이드 04 12-6). 하나라도 어긋나면 1로 끝납니다.
#   1. 기대값: sql/silver/checks/expected.sql
#   2. 같은 입력 다시 넣기: silver.sh를 한 번 더 돌린 뒤 직전 snapshot과 모든 칸이 같은가
#   3. 과거 변경 다시 넣기: 옛 c를 새 offset으로 MERGE해도 버전 가드가 거절해 모든 칸이 같은가
# Silver를 이미 한 번 만든 뒤에 돌립니다(infra/spark/silver.sh).
set -euo pipefail
cd "$(dirname "$0")/../.."

tables=(orders executions holdings daily_prices fx_rates)
q() { SQL_FORMAT=tsv2 infra/spark/sql.sh -e "$1" 2>/dev/null | grep -Eo '^-?[0-9]+$' | tail -1 || true; }

# 테이블의 현재 snapshot id. 아직 commit이 없으면 빈 글자
current() { q "SELECT snapshot_id FROM lake.silver.$1.snapshots ORDER BY committed_at DESC LIMIT 1"; }
snapshots() { q "SELECT count(*) FROM lake.silver.$1.snapshots"; }

# snapshot $2와 지금 사이에 한쪽에만 있는 행 수
changed_rows() {
  local t=$1 id=$2
  if [ -z "$id" ]; then q "SELECT count(*) FROM lake.silver.$t"; return; fi
  q "SELECT count(*) FROM (
       (SELECT * FROM lake.silver.$t VERSION AS OF $id EXCEPT SELECT * FROM lake.silver.$t)
       UNION ALL
       (SELECT * FROM lake.silver.$t EXCEPT SELECT * FROM lake.silver.$t VERSION AS OF $id))"
}

# 모든 테이블의 (snapshot id, snapshot 수)를 적어 두고, 단계 뒤 바뀐 행을 셉니다.
# macOS 기본 bash(3.2)에도 돌게 연관 배열 대신 tables와 같은 순서의 배열을 씁니다.
before_id=() before_n=()
mark() {
  local i
  for i in "${!tables[@]}"; do
    before_id[$i]=$(current "${tables[$i]}")
    before_n[$i]=$(snapshots "${tables[$i]}")
  done
}
compare() {
  local label=$1 bad=0 i t n
  printf '%-10s %-13s %8s   %s\n' "$label" table changed "snapshots(전 -> 후)"
  for i in "${!tables[@]}"; do
    t=${tables[$i]}
    n=$(changed_rows "$t" "${before_id[$i]}")
    printf '%-10s %-13s %8s   %s -> %s\n' "" "$t" "$n" "${before_n[$i]:-0}" "$(snapshots "$t")"
    [ "$n" = "0" ] || bad=1
  done
  return $bad
}

fail=0

echo "## 1. 기대값"
infra/spark/sql.sh sql/silver/checks/expected.sql >/dev/null && echo "통과" || { echo "실패"; fail=1; }

echo "## 2. 같은 입력 다시 넣기"
mark
infra/spark/silver.sh >/dev/null
compare "rerun" || fail=1

echo "## 3. 과거 변경 다시 넣기(버전 가드)"
mark
infra/spark/sql.sh \
  sql/silver/orders/1_incoming.sql sql/silver/holdings/1_incoming.sql \
  sql/silver/checks/redeliver_old.sql \
  sql/silver/orders/2_merge.sql sql/silver/holdings/2_merge.sql >/dev/null
compare "redeliver" || fail=1
infra/spark/sql.sh sql/silver/checks/expected.sql >/dev/null && echo "기대값 다시 통과" || { echo "기대값 실패"; fail=1; }

[ $fail = 0 ] && echo "Silver 검증 통과" || echo "Silver 검증 실패"
exit $fail
