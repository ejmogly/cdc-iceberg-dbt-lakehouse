# 손익 SQL (dbt Gold 모델 초안)

E01([`docs/ai/experiments/E01-pnl.md`](../../docs/ai/experiments/E01-pnl.md))의 계산 규칙 R01 ~ R15를 SQL로 옮긴 것입니다. 파일 하나가 dbt 모델 하나이고, 다른 모델은 `{{ ref('...') }}`로 부릅니다. 돌리는 길은 둘입니다.

- [`eval/e01_pnl/check_sql.py`](../../eval/e01_pnl/check_sql.py): `ref()`를 뷰 이름으로 바꿔 DuckDB에서 돌립니다(`make check`).
- [`eval/e01_pnl/run_dbt.py`](../../eval/e01_pnl/run_dbt.py): 같은 파일을 dbt(DuckDB 어댑터)로 빌드합니다(아래 「dbt로 돌리기」).

파이프라인의 Spark 3.5.6에서는 재귀 CTE가 안 돼 아직 돌리지 않습니다(엔진 함정 4).

## 검증 (2026-11-11, DuckDB 2.0.0)

30개를 대조하고 결과를 `eval/e01_pnl/sql_check_result.json`에 씁니다. `--show G13`은 한 사례의 장부를 보여 줍니다.

```bash
.venv/bin/python eval/e01_pnl/check_sql.py
.venv/bin/python eval/e01_pnl/check_sql.py \
  --show G13
```

| 확인 | E01 통과선 | 결과 |
|---|---|---|
| 정답 칸(수량, 평균 단가, 평균 환율, 실현, 평가, 합계, 매수 합계, 단순 수익률, 세 효과, 기준선, 낙폭)과 매도 한 건씩 | 전부 정확히 일치 | 30/30 |
| 연 환산(`irr_pct`, `bench_irr_pct`) | 0.05%p 안 | 50칸 모두, 가장 큰 차이 0.01%p |
| 항등식(반올림 전, 합계는 현금 흐름으로 따로 냄) | 0.01원 미만 | 30/30, 가장 큰 차이 2.3e-10원 |
| 경고 코드와 줄 번호 | 전부 일치 | 30/30 |
| 입력 순서 섞기(같은 날 안의 순서는 유지) | 30개 모두 | 사례마다 5번, 30/30 |
| 두 번 돌리기 | 같음 | 같음 |
| 기준선 칸(`naive_krw`, 참고) | | 30/30 |

평가 기준과 정답은 바꾸지 않았습니다. 첫 실행에서 6개가 어긋났고 원인은 SQL 쪽이었습니다(아래 「엔진 함정」 1). 반올림 전 맞춤을 빼고 돌려 본 시험에서 하나가 더 드러나 고쳤습니다(2).

**규모 시험(참고).** 가짜 시세로 만든 체결 10만 건(계산 단위 5,000개 x 20건, 1년 달력)을 같은 SQL로 돌렸더니 DuckDB에서 장부(재귀) 한 번에 약 10초, 모든 모델을 뷰 그대로 차례로 만들어 36초였습니다. E01의 지연 통과선(dbt 전체 빌드 5분)은 아래 dbt 실행에서 쟀습니다.

## dbt로 돌리기 (2026-11-12, E01 본 실행)

이 파일들을 고치지 않고 dbt(dbt-core 1.12.6, dbt-duckdb 1.11.1)로 빌드합니다. 프로젝트는 [`eval/e01_pnl/dbt/`](../../eval/e01_pnl/dbt/)입니다.

- 실행기가 빌드 전마다 이 폴더의 `*.sql`을 `models/pnl/`(git 제외)로 복사합니다.
  - 프로젝트 밖 경로를 `model-paths`에 넣으면 dbt가 컴파일한 SQL을 `target` 밖에 씁니다.
  - 디렉터리 링크는 dbt가 따라가지 않습니다.
- 입력 6개는 얇은 모델로 감쌉니다.
- 통과선은 dbt 테스트 4개(`e01_numbers`, `e01_sells`, `e01_warnings`, `e01_identity`)입니다.

```bash
.venv/bin/python eval/e01_pnl/run_dbt.py
.venv/bin/python eval/e01_pnl/run_dbt.py --scale
```

| 실행 | 결과 |
|---|---|
| 골든(사례 30, 섞은 사본 150, 두 번) | 테스트 7개 통과, 섞기 30/30, 다시 실행 같음, 빌드 4.3초 |
| 규모(체결 10만 건, 계산 단위 5,000) | 항등식과 키 테스트 통과, 빌드 5.6초(테이블로 만듦), 계산 단위 하나 조회 p95 0.88ms |

자세한 것은 [E01](../../docs/ai/experiments/E01-pnl.md) 4절과 5절에 있습니다.

## 모델

| 모델 | 한 행 | 하는 일 |
|---|---|---|
| `pnl_market_daily` | 종목 x 달력 날 | 직전 값으로 채운 종가, 그날 저가와 고가, 원/달러 |
| `pnl_position_base` | 계산 단위 | 통화, 기준 종목, 달력에 맞춘 기준일 |
| `pnl_trade_prep` | 매매 줄 | 계산 날짜, 가격, 환율을 채우고 입력 경고, 처리 순서 `step` |
| `pnl_ledger` | 매매 줄 | **재귀 CTE**로 이동평균 장부(수량, 현지 원가, 원화 원가, 기준 종목 단위) |
| `pnl_trade_result` | 매매 줄 | 매수 금액, 실현 손익, 효과 나누기, 기준 종목의 같은 매매 |
| `pnl_daily` | 계산 단위 x 달력 날 | 누적 손익과 고점(최대 낙폭용) |
| `pnl_position_end` | 계산 단위 | 기준일의 수량, 원가, 종가, 환율 |
| `pnl_irr` | 계산 단위 | 연 환산 수익률(격자 두 번 좁히기와 직선 맞춤) |
| `pnl_position` | 계산 단위 | 요약. 칸 이름이 E01 정답과 같음. 항등식 검사용 `gap_*` |
| `pnl_sells` | 매도 한 건 | 매도일, 수량, 체결가, 직전 평균 단가, 실현 손익 |
| `pnl_warnings` | 경고 하나 | `BAD_QTY`, `AFTER_ASOF`, `OVERSOLD`, `NON_TRADING_DAY`, `PRICE_OUT_OF_RANGE` |

## 입력 (5회차에 staging 모델로 만들 것)

| 입력 | 칸 | 평가셋에서 | 실제 데이터에서 |
|---|---|---|---|
| `pnl_calendar` | `cal_date` | fixture의 달력 13일 | 날짜 축 전체(시세와 환율이 있는 날을 모두 담아야 함) |
| `pnl_prices` | `symbol`, `trade_date`, `close_price`, `low_price`, `high_price` | fixture 시세 | Silver `daily_prices` |
| `pnl_fx` | `rate_date`, `rate` | fixture 환율 | Silver `fx_rates`의 `USDKRW`. 미국 거래일과 맞추는 규칙은 정할 것([`docs/ai/data-prep.md`](../../docs/ai/data-prep.md)) |
| `pnl_instruments` | `symbol`, `currency` | `US1`, `KR1`, `BENCH` | `loaders/instruments.csv`(seed) |
| `pnl_positions` | `position_id`, `symbol`, `asof_date`, `bench_symbol` | 사례 30개 | (`account_id`, `symbol`)마다 하나, 기준일은 실행일, 기준 종목 `SPY` |
| `pnl_trades` | `position_id`, `trade_seq`, `trade_date`, `side`, `qty`, `price`, `fx_rate` | 사례의 매매 줄(번호는 입력 순서) | Silver `executions` ⋈ `orders`. `trade_date`는 `executed_at`을 그 시장 현지 날짜로, `trade_seq`는 (`executed_at`, `execution_id`) 순서(R05), `price`는 `exec_price`, `fx_rate`는 `executions.fx_rate` |

## 엔진 함정 (5회차에 옮길 때 볼 것)

1. **CASE 갈래의 자료형이 섞이면 결과가 바뀝니다.** DuckDB는 `decimal(38,2)`와 `decimal(38,0)` 갈래를 `decimal(38,0)`으로 합쳐 달러 손익의 소수를 버렸습니다(첫 실행에서 6개 실패). 갈래마다 같은 자료형으로 바꿉니다.
2. **나눗셈과 double.** DuckDB의 decimal 나눗셈은 double이고, double을 `decimal(38,18)`로 바꿀 때도 오차가 붙습니다(700000 → 700000.000000000041943040). 나눗셈 갈래가 섞인 CASE는 매수 갈래까지 double이 되어 정확해야 할 원가에 오차가 붙었고, 반올림 전 맞춤 없이 돌리면 .5 경계 사례(G30)가 1원 어긋났습니다. 그래서 나눗셈은 갈래 안에서 decimal로 바꾸고, 전량 매도는 나누지 않고 원가를 그대로 덜어 냅니다. 마지막 반올림 전에 소수 여섯째 자리로 한 번 맞추는 것은 남은 안전장치입니다(지금 30개는 이것 없이도 통과).
3. **decimal끼리 곱하면 자리가 넘칩니다.** `decimal(38,18) x decimal(38,18)`은 DuckDB에서 오류입니다. 평균 단가와 평균 환율을 곱하는 효과 나누기와 기준 종목은 double로 셉니다. Spark는 반대로 자리를 줄여(`allowPrecisionLoss`) 소수 여섯째 자리까지만 남길 수 있으니, 옮긴 뒤 같은 30개로 다시 확인합니다.
4. **재귀 CTE.** DuckDB와 PostgreSQL은 됩니다. Spark SQL은 판에 따라 다르므로(최근 판에서 `WITH RECURSIVE`를 더했다고 알려졌지만 이 저장소의 판으로는 확인하지 않음) 5회차 버전을 정할 때 먼저 봅니다. 안 되면 후보는 `aggregate` 고차 함수(계산 단위마다 매매를 배열로 모아 접기), dbt Python 모델, 증분 상태 테이블입니다.
5. **반올림.** R15는 0.5를 0에서 먼 쪽으로 보냅니다. decimal의 `round`는 DuckDB, PostgreSQL, Spark 모두 그렇지만, PostgreSQL의 double `round`와 Spark의 `bround`는 짝수 쪽입니다. 그래서 반올림 전에 decimal로 바꿉니다. `round`의 자릿수는 상수여야 합니다(DuckDB).
6. **연 환산.** 이분법 반복은 재귀 CTE 안에서 집계를 해야 해서 엔진마다 막힙니다. 대신 x = ln(1 + r)에 101점 격자를 두 번 깔아 좁히고 마지막 칸에서 직선으로 맞춥니다. 부호가 바뀌는 칸이 여럿이면 가장 낮은 쪽을 고르므로, 흐름의 부호가 여러 번 바뀌는 드문 경우에는 이분법과 다른 근을 고를 수 있습니다.
7. **dbt 문법.** 날짜 차는 `{{ dbt.datediff(...) }}`로 썼습니다. `cast(... as double)`과 `(values ...)` 인라인 테이블은 엔진마다 쓰는 법이 조금 달라, 옮길 때 dbt의 형 변환 매크로나 `dbt_utils.generate_series`로 바꿀 수 있습니다.
