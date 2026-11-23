# 키와 Docker 없이 도는 확인. CI(.github/workflows/check.yml)도 같은 목표를 씁니다.
PY ?= .venv/bin/python

.PHONY: setup check check-pnl check-public check-history screen-data screen-eval-data window-fetch window-load window-extend lake-auto-on lake-auto-off screen-golden screen screen-example check-screen trades-to-source trades-verify check-pnl-spark lake-refresh lake-refresh-test source-migrate

PYTHON ?= python3

# 패키지(duckdb 2.0 등)가 Python 3.11 이상을 요구합니다. 다른 이름의 파이썬은 make setup PYTHON=python3.14
setup:
	@$(PYTHON) -c 'import sys; v = sys.version_info; sys.exit(0 if v >= (3, 11) else f"Python 3.11 이상이 필요합니다(지금 {v.major}.{v.minor}). 예: make setup PYTHON=python3.14")'
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -r loaders/requirements.txt

# 이미 떠 있는 원천 DB에 infra/postgres/migrate/의 SQL을 번호 순서로(여러 번 돌려도 같음). 새 DB는 init/이 같은 일을 함
source-migrate:
	for f in infra/postgres/migrate/*.sql; do echo "== $$f"; \
	  docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < $$f || exit 1; done

check: check-pnl check-public

# 손익 SQL을 E01 골든 30개와 대조(DuckDB, 가짜 시세)
check-pnl:
	$(PY) eval/e01_pnl/check_sql.py --no-save

# 같은 손익 모델의 Spark 판(dbt/models/pnl)을 골든 30개와 섞은 사본 150개로 맞춤(Spark 필요, CI에는 없음)
check-pnl-spark:
	$(PY) eval/e01_pnl/check_spark.py

# 가운뎃점, 글자에 붙은 물결표, 개인 경로, 비밀값, 금지어
check-public:
	$(PY) scripts/check_public.py

# 공개 전에 한 번: git 기록 전체
check-history:
	$(PY) scripts/check_public.py --history

# F 화면(docs/ai/F-screen.md). 아래는 받은 원본(data/raw/)이 있는 이 컴퓨터에서만 돕니다(CI에는 없음).
# Gold 마트(dbt mart_symbol_daily, mart_fx_daily)에서 화면 데이터 파일로 내보내고 원천과 원본에 대조.
# VM, 원천 DB, Spark(--profile spark)가 떠 있고 Silver MERGE와 dbt build를 먼저 돌려야 함
screen-data:
	$(PY) -m ai.screen_data export

# 평가용 화면 파일(screen_eval.duckdb, 정답을 고정한 때의 2년치)을 첫 구간 원본에서 다시 만들고 내용 지문을 대조.
# 평가 시험(check_api, check_screen 등)은 이 파일을 엶. 화면은 구간 목록(data/generated/window.json)의 구간을 모두 담은 screen.duckdb
screen-eval-data:
	$(PY) -m ai.screen_data freeze

# 화면 데이터 기간 늘리기(docs/decisions/0016). 받기는 Mac, 처리는 Airflow DAG window_extend.
# window-fetch: 국내 시세가 나온 마지막 거래일까지(END=YYYY-MM-DD로 앞당김) 새 구간 원본을 data/raw/window/에 받고 next.json을 적음.
#   키(.env)만 필요하고 레이크는 꺼져 있어도 됨. 할 일만 보려면 .venv/bin/python loaders/extend_window.py fetch --plan
# window-load: 받아 둔 원본이 있으면 window_extend를 지금 한 번(확인, 원천 적재, Bronze 기다림, 구간 더하기, 새로 고침, 화면 확인).
#   레이크와 Bronze 작업이 떠 있어야 함. lakehouse_refresh가 꺼져 있으면 새로 고침을 건너뛰므로 그때는 make lake-refresh-test
# window-extend: 둘을 차례로
window-fetch:
	$(PY) loaders/extend_window.py fetch $(if $(END),--end $(END))

window-load:
	@rc=0; $(PY) loaders/extend_window.py pending || rc=$$?; \
	if [ $$rc -eq 0 ]; then docker compose exec -T airflow airflow dags test window_extend; \
	elif [ $$rc -ne 99 ]; then exit $$rc; fi

window-extend: window-fetch window-load

# Airflow DAG 두 개(lakehouse_refresh 30분마다, window_extend 매일 14:40)를 켜거나 끔. 레이크를 켜 둔 동안만 돎
lake-auto-on:
	docker compose exec -T airflow bash -c 'airflow dags unpause lakehouse_refresh && airflow dags unpause window_extend'

lake-auto-off:
	docker compose exec -T airflow bash -c 'airflow dags pause window_extend && airflow dags pause lakehouse_refresh'

# 화면에 저장한 매매를 원천 주문, 체결, 잔고(계좌 A-SCREEN)로 옮김(VM 필요). 그 뒤 Silver MERGE, dbt build, trades-verify
trades-to-source:
	$(PY) loaders/screen_trades.py

# Gold(dbt fct_execution)가 저장한 매매와 같은지, Gold 체결로 다시 센 손익이 화면 손익과 같은지(Spark 필요). 화면 데이터 탭에 보임
trades-verify:
	$(PY) loaders/screen_trades.py --verify

# 위 일을 Airflow DAG lakehouse_refresh로 지금 한 번(30분 주기를 기다리지 않음). 시험용 매매 파일은 TRADES=data/generated/f_screen/<파일>.json
# docker compose --profile flink --profile spark --profile airflow up -d 와 Bronze 작업이 떠 있어야 함. DAG 멈춤은 airflow dags unpause로 풂
lake-refresh:
	docker compose exec -T airflow airflow dags trigger lakehouse_refresh $(if $(TRADES),--conf '{"trades": "$(TRADES)"}')

# 같되, DAG가 꺼져 있을 때 이 자리에서 끝까지 돌림(airflow dags test, 로그가 바로 보임). dags test는 max_active_runs를 지키지 않아
# 예약 실행과 겹치면 두 Silver MERGE가 한 Spark 세션의 같은 임시 뷰 이름을 써서 구간이 빠질 수 있으므로, DAG가 켜져 있으면 멈춤
lake-refresh-test:
	@paused=$$(docker compose exec -T airflow airflow dags list -o plain 2>/dev/null | awk '$$1 == "lakehouse_refresh" {print $$NF}'); \
	if [ "$$paused" != "True" ]; then \
	  echo "lakehouse_refresh가 켜져 있어(is_paused=$$paused) 예약 실행과 겹칠 수 있습니다. 켜진 동안에는 make lake-refresh(한 번에 하나만 돎)를 쓰고, 이 시험은 make lake-auto-off 뒤에 돌립니다"; \
	  exit 1; fi
	docker compose exec -T airflow airflow dags test lakehouse_refresh $(if $(TRADES),--conf '{"trades": "$(TRADES)"}')

# E01 골든의 가짜 시세로 같은 구조의 파일(골든 대조용)
screen-golden:
	$(PY) -m ai.screen_data golden

# 화면 서버(VM 필요 없음). 브라우저로 http://127.0.0.1:8765/ 를 엶. 넣은 매매는 data/generated/f_screen/에 저장
screen:
	$(PY) -m ai.screen_server

# 저장을 끈 예시 화면(8766). 넣어 둔 매매 파일을 건드리지 않고, 매매가 없는 종목은 예시 매매로 보임
screen-example:
	$(PY) -m ai.screen_server --port 8766 --trades ""

# 내보내기 검사 자체 시험, E04 기록 재생 회귀, 계산 서버 대조(골든, 실제, 지연, 파이프라인 상태, 입력 경계),
# 범위 칸 대조(8번 칸: 독립 정답 38행, 미래 바꾸기 시험, 화면 글자 38행), 화면 값 대조(헤드리스 Chrome으로 그린 화면의 글자가 API 값과
# 같은지, 색 대비), 입력 흐름(빠른 고침, 저장 실패 등), 묻고 답하기 갈래(9번 칸: 모델 없이 꾸민 분류와 답)
check-screen:
	$(PY) -m ai.screen_data --selftest
	$(PY) loaders/screen_trades.py --selftest
	$(PY) loaders/load_test.py --selftest
	$(PY) eval/f_screen/replay_e04.py
	$(PY) eval/f_screen/replay_e04.py --selftest
	$(PY) eval/f_screen/check_api.py
	$(PY) eval/f_screen/check_qa.py --selftest
	$(PY) eval/f_screen/check_qa.py
	$(PY) eval/f_screen/check_range.py --selftest
	$(PY) eval/f_screen/check_range.py
	$(PY) eval/f_screen/check_screen.py --selftest
	$(PY) eval/f_screen/check_screen.py
	$(PY) eval/f_screen/check_flows.py --selftest
	$(PY) eval/f_screen/check_flows.py
