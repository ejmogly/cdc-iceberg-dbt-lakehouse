# 처음부터 다시 올려 보기: 새로 받은 저장소에서 README대로 (2026-11-24)

공개본을 받은 사람이 README만 보고 끝까지 갈 수 있는지, 새로 받은 저장소(커밋 `feafc9b`)에서 README의 명령을 차례로 돌려 봤습니다. [독립 검토](2026-11-20_independent-review.md) 1절(README만으로 따라가기)의 고친 것을 실제로 다시 확인하는 일입니다. 원래 11/26 ~ 11/27이던 일을 담당자가 「오늘 해」로 앞당겼습니다.

## 요약

| 단계 | 결과 | 걸린 시간 |
|---|---|---|
| `make setup` | 됨(Python 3.14.7, PyPI에서 받음) | 17초 |
| `make check` | 손익 골든 30/30, 공개 검사 0건(건너뛴 검사 셋을 끝에 밝힘) | 60초 |
| 가짜 시세 화면(`make screen-golden`과 화면 서버) | 첫 화면 200, 상태 통과 | 몇 초 |
| `docker compose up -d --wait`(기본 구성) | 됨 | 22초 |
| `infra/connect/register.sh` | **막힘 → 고침.** 등록 직후 상태 조회가 404로 끝나 README의 「마지막 줄이 RUNNING」과 달랐음. RUNNING까지 30초 안에서 기다리게 고침 | 3초 |
| 시나리오 01 ~ 06과 `check.sql` | 원천 주문 2, 체결 2, 잔고 0행, 토픽 6, 2, 3개(README 기대값과 같음) | 몇 초 |
| 레이크 `up -d --wait`와 `submit-bronze.sh` | 됨. Bronze 주문 6행이 작업을 보낸 뒤 14초 안에 보임(README의 10 ~ 20초) | 18초 + 14초 |
| `silver.sh`, `check-silver.sh`, `dbt build`, Silver 잔고, 증분 Gold `--compare` | 검증 통과, `dbt build` 95/95(시세 없이), 유효 잔고 0행, 비교 0 / 0 | 1분 27초 |
| Airflow `up -d --wait`와 `dags unpause` | **막힘 → 고침.** 컨테이너가 뜨자마자 `--wait`가 끝나, 바로 친 `unpause`가 「initialize the database」로 실패. Airflow 서비스에 상태 검사(`/health`의 메타 DB가 healthy이고 scheduler가 한 번이라도 뛴 것)를 더해, 볼륨을 지우고 다시 올리자 21초 기다린 뒤 `unpause`가 바로 됨. 마지막 검토에서 「scheduler healthy」는 긴 태스크 동안 풀릴 수 있다는 지적을 받아 지금 조건으로 바꾸고 새 볼륨에서 다시 확인함 | 21초 |
| `lakehouse_refresh` 첫 실행 | Silver, dbt, 증분 Gold 성공. 화면 파일 내보내기와 화면 매매 대조는 「건너뜀」(받은 원본 39개 없음, 매매 파일 없음) | 약 1분 |
| `make lake-auto-on`의 `window_extend` | `wait_raw`가 「받아 둔 새 원본이 없습니다」로 기다림(reschedule). `make lake-auto-off`로 끔 | |
| 끄기(`stop-bronze.sh`, `compose stop`) | 됨. savepoint를 남김 | 몇 초 |

막힌 곳 둘은 이 저장소에서 고친 뒤 새 저장소에 그 파일을 넣어 다시 확인했습니다. 그 밖에 README에 두 줄(`COMPOSE_PROJECT_NAME`의 뜻, Airflow가 처음 뜰 때 기다리는 까닭)을 더하고, 기간 늘리기의 안내 글을 「호스트의 `make window-fetch`를 기다림」으로 바꿨습니다.

## 측정 조건

| 무엇 | 값 |
|---|---|
| 기기와 자원 | Apple Silicon Mac 한 대, Colima VM(CPU 6, 메모리 12GB, 디스크 60GB) |
| 버전 | README 「버전」 표 그대로. 파이썬은 Homebrew Python 3.14.7 |
| 데이터 | 합성 시나리오 01 ~ 06만. 받은 원본(`data/raw/`)과 API 키 없음 |
| 저장소 | 이 저장소를 홈 폴더 아래에 로컬로 새로 받음(`git clone`, 커밋 `feafc9b`). `data/`에는 README만 있음 |
| 바꾼 값 | `.env`의 비밀번호 넷을 시험용 임의 값으로, `COMPOSE_PROJECT_NAME`을 `cdcfresh`로(쓰던 레이크 `cdclake`와 볼륨을 나누려고). 화면 서버는 쓰던 서버가 8765에 있어 다른 포트로 |
| 반복 | 한 번. 시간은 명령 앞뒤의 시각에서 셈 |
| 정리 | 끝난 뒤 `cdcfresh` 프로젝트의 컨테이너와 볼륨만 지움(`docker compose -p cdcfresh ... down -v`). 쓰던 레이크의 볼륨 6개는 그대로 |

## 확인하지 못한 것

- **이미지 빌드.** 같은 태그(`cdclake/...`)의 이미지가 이미 있어 compose가 다시 빌드하지 않았습니다. Dockerfile에서 Maven Central(jar, sha256 대조)과 PyPI에서 받는 길은 11/12 뒤로 다시 돌리지 않았습니다.
- amd64, Linux와 Windows, Docker Desktop.
- 키를 넣고 실제 시세와 기사를 받는 길(이 저장소에서는 평일 14:30에 돌고 있음).
- 처음 뜬 Airflow의 화면 로그인(비밀번호 파일이 생긴 것만 봄).
