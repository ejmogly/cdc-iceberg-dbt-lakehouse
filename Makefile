# 키와 Docker 없이 도는 확인. CI(.github/workflows/check.yml)도 같은 목표를 씁니다.
PY ?= .venv/bin/python

.PHONY: setup check check-pnl check-public check-history screen-data screen-golden screen check-screen

setup:
	python3 -m venv .venv
	.venv/bin/pip install -r loaders/requirements.txt

check: check-pnl check-public

# 손익 SQL을 E01 골든 30개와 대조(DuckDB, 가짜 시세)
check-pnl:
	$(PY) eval/e01_pnl/check_sql.py --no-save

# 가운뎃점, 글자에 붙은 물결표, 개인 경로, 비밀값, 금지어
check-public:
	$(PY) scripts/check_public.py

# 공개 전에 한 번: git 기록 전체
check-history:
	$(PY) scripts/check_public.py --history

# F 화면(docs/ai/F-screen.md). 아래는 받은 원본(data/raw/)이 있는 이 컴퓨터에서만 돕니다(CI에는 없음).
# 원천 PostgreSQL에서 화면 데이터 파일로 내보내고 검사. VM과 원천 DB가 떠 있어야 함
screen-data:
	$(PY) -m ai.screen_data export

# E01 골든의 가짜 시세로 같은 구조의 파일(골든 대조용)
screen-golden:
	$(PY) -m ai.screen_data golden

# 화면 서버(VM 필요 없음). http://127.0.0.1:8765/
screen:
	$(PY) -m ai.screen_server

# 내보내기 검사 자체 시험, E04 기록 재생 회귀, 계산 서버 대조(골든, 실제, 지연, 파이프라인 상태)
check-screen:
	$(PY) -m ai.screen_data --selftest
	$(PY) eval/f_screen/replay_e04.py
	$(PY) eval/f_screen/replay_e04.py --selftest
	$(PY) eval/f_screen/check_api.py
