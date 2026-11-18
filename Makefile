# 키와 Docker 없이 도는 확인. CI(.github/workflows/check.yml)도 같은 목표를 씁니다.
PY ?= .venv/bin/python

.PHONY: setup check check-pnl check-public check-history screen-data screen-golden screen screen-example check-screen trades-to-source trades-verify check-pnl-spark lake-refresh

setup:
	python3 -m venv .venv
	.venv/bin/pip install -r loaders/requirements.txt

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
