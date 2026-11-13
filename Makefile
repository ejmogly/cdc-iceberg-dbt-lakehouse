# 키와 Docker 없이 도는 확인. CI(.github/workflows/check.yml)도 같은 목표를 씁니다.
PY ?= .venv/bin/python

.PHONY: setup check check-pnl check-public check-history

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
