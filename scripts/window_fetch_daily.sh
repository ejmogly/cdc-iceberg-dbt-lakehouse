#!/usr/bin/env bash
# 평일 14:30에 Mac의 launchd가 부르는 기간 늘리기 받기(docs/decisions/0016).
# 저장소 루트에서 `make window-fetch`를 돌리고, 출력은 data/generated/logs/window_fetch.log(git 밖)에 덧붙입니다.
# 받을 날이 없으면 그대로 끝나고(종료 코드 0), 실패하면 macOS 알림을 띄웁니다(next.json은 바뀌지 않음).
# 국내 시세는 다음 영업일 13시 뒤에 나오므로 평일 14:30이면 전 영업일까지 받습니다. 처리는 레이크를 켰을 때 Airflow window_extend가 합니다.
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
log=data/generated/logs/window_fetch.log
mkdir -p "$(dirname "$log")"

echo "== $(date '+%Y-%m-%d %H:%M:%S') 받기 시작" >>"$log"
make window-fetch >>"$log" 2>&1
rc=$?
echo "== $(date '+%Y-%m-%d %H:%M:%S') 끝(종료 코드 $rc)" >>"$log"

if [ "$rc" -ne 0 ]; then
  osascript -e "display notification \"종료 코드 $rc, 기록은 $log\" with title \"기간 늘리기 받기 실패\"" >/dev/null 2>&1 || true
fi
exit "$rc"
