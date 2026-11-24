# 0004 숫자 자료형과 커넥터의 decimal 처리

- 날짜: 2026-11-06(자료형), 2026-11-11(커넥터 설정)
- 상태: 결정

## 맥락

국내 종목 가격은 원 단위 정수이고, 미국 종목 가격은 달러 소수입니다. 같은 칸에 두 통화가 들어옵니다.
손익은 원 단위까지 정확히 맞아야 합니다(E01 통과선).

Debezium PostgreSQL 커넥터의 기본값 `decimal.handling.mode=precise`에서는 `numeric` 칸이 JSON(스키마 없음)에서 숫자가 아니라 base64 글자로 나옵니다.
값을 읽으려면 스키마의 소수 자릿수를 따로 알아야 합니다.

## 결정

1. 가격과 금액은 `numeric(18,4)`, 환율은 `numeric(12,4)`입니다.
2. 커넥터에 `decimal.handling.mode=string`을 넣습니다. `70000.0000`처럼 글자 그대로 나오고, Silver에서 decimal로 바꿉니다.
   - **원천에 행을 넣기 전에** 커넥터가 이 설정으로 등록돼 있어야 합니다. 그 전에 나간 이벤트는 base64로 남습니다.
3. 거래일은 `date`입니다. Debezium은 1970-01-01부터 센 날 수(정수)로 내보내므로 Silver에서 날짜로 바꿉니다.
4. 시각은 `timestamptz`입니다. UTC ISO 8601 글자로 나와 읽기 쉽습니다.
5. 상태와 방향은 PostgreSQL `enum` 대신 `text`와 `CHECK`로 둡니다.
6. 통화 칸은 두지 않습니다. 종목이 통화를 정하므로 `instruments.csv`에서 붙입니다.

## 검토한 다른 방법

| 방법 | 고르지 않은 이유 |
|---|---|
| 커넥터 기본값(`precise`) | base64를 풀려면 소수 자릿수를 따로 들고 다녀야 하고, Bronze에서 값을 눈으로 확인할 수 없음 |
| `decimal.handling.mode=double` | 소수 오차가 생겨 원 단위 정확 일치 목표와 충돌함 |
| 센트, 원 단위 정수(`bigint`) | 통화마다 자릿수가 달라 칸의 뜻이 헷갈림. 1판에서 원 단위 `bigint`를 썼다가 바꿈 |
| `enum` | 값을 더하거나 바꿀 때 타입 변경이 필요함 |

## 결과

- Bronze의 가격은 글자이므로 Silver에서 자료형을 바꿉니다. 변환 실패를 세지 않고, ANSI 모드(`spark.sql.ansi.enabled`)로 첫 실패에서 멈춥니다(4회차).
- 손익 SQL은 원가와 금액을 decimal로 들고 다니되, 나눗셈(평균 단가, 평균 환율)과 효과 나누기, 기준 종목은 일부러 double로 셉니다. 표시 전에 소수 여섯째 자리로 맞춘 뒤 decimal로 바꿔 반올림합니다. 엔진마다 decimal 나눗셈과 CASE의 자료형 합치기가 달라서이고(DuckDB는 자리 넘침 오류, Spark는 decimal 나눗셈의 자리 줄임으로 1원 차이, [0014](0014-portfolio-tab-and-account-mart.md)), 함정은 [`sql/pnl/README.md`](../../sql/pnl/README.md) 「엔진 함정」에 적었습니다. 모두 decimal로 되돌리면 그 차이가 다시 납니다(`make check-pnl-spark`가 잡음).
- 커넥터 설정은 [`infra/connect/source-postgres.json`](../../infra/connect/source-postgres.json), 테이블은 [`infra/postgres/init/10_brokerage.sql`](../../infra/postgres/init/10_brokerage.sql)입니다.
