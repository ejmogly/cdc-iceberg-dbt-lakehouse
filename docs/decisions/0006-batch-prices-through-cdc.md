# 0006 배치 데이터도 원천 DB를 거쳐 CDC로, 값이 바뀐 행만 고침

- 날짜: 2026-11-06(경로), 2026-11-11(적재 방식)
- 상태: 결정, 확인함(2026-11-12 PostgreSQL, 아래 「결과」). 11/13부터 범위는 시세와 환율(아래 「범위가 바뀜」)

## 맥락

시세, 환율, 뉴스, 관심도는 앱이 아니라 배치 적재기가 하루 한 번 받는 데이터입니다. 레이크에 바로 써도 됩니다.
하지만 제공처는 지난 값을 고치기도 합니다(종가 정정, 액면분할 뒤 수정주가). 그 경위가 남아야 6회차에 "지난 날 지표가 왜 바뀌었나"를 설명할 수 있습니다.

적재기는 늦게 고쳐진 값을 잡으려고 매일 최근 며칠을 다시 받습니다. 그런데 PostgreSQL은 같은 값으로 `UPDATE`해도 새 행 판을 쓰므로, Debezium이 `u` 이벤트를 냅니다.
그대로 두면 바뀐 것이 없는데도 매일 시세만 약 95개(19종목 x 5거래일)의 `u`가 생기고, 6회차의 영향 범위가 부풀어 오릅니다.

## 결정

1. 배치 데이터도 원천 PostgreSQL의 `source` 스키마에 넣고, 주문과 같은 CDC 길로 Bronze까지 보냅니다.
2. publication을 `FOR TABLES IN SCHEMA source`로 두어, 새 테이블이 저절로 CDC 대상이 됩니다.
3. 적재는 **값이 바뀐 행만 고치는** upsert로 합니다.

```sql
INSERT INTO source.daily_prices AS p
VALUES (...)
ON CONFLICT (symbol, trade_date)
DO UPDATE SET
  close_price = EXCLUDED.close_price,
  -- 다른 값 칸, loaded_at
  ...
WHERE (p.open_price, p.high_price,
       p.low_price, p.close_price,
       p.volume)
  IS DISTINCT FROM
      (EXCLUDED.open_price,
       EXCLUDED.high_price,
       EXCLUDED.low_price,
       EXCLUDED.close_price,
       EXCLUDED.volume);
```

4. 비교에는 값 칸만 넣고 `loaded_at` 같은 적재 정보 칸은 뺍니다. 시세, 환율, 뉴스, 관심도 적재기가 모두 같은 방식입니다.

## 검토한 다른 방법

| 방법 | 고르지 않은 이유 |
|---|---|
| 레이크에 배치로 바로 쓰기 | 들어오는 길이 둘이 되어 운영할 것이 늘고, 제공처 정정의 경위가 덮어써짐 |
| 조건 없는 upsert | 같은 값이어도 `u` 이벤트가 생김 |
| 지우고 다시 넣기 | 다시 받을 때마다 `d`와 `c` 쌍이 생겨 조건 없는 upsert보다 나쁨 |

## 결과

- 시험(2026-11-11): 실제로 받은 시세와 환율 원본을 적재기에 넣되, PostgreSQL 대신 DuckDB로 옮겨 돌렸습니다(DDL 두 곳과 SQL 자리표시만 고침).
  - 처음 넣을 때는 모두 삽입됐습니다.
  - 같은 원본을 다시 넣으면 고친 행이 0이었습니다.
  - 종가 하나만 바꿔 넣으면 1행만 고쳐졌습니다.
- PostgreSQL에서 같은 결과와 Kafka 메시지 수를 2026-11-12에 확인했습니다. 다시 적재하면 고친 행 0, 토픽 끝 위치 그대로입니다([`docs/ai/data-prep.md`](../ai/data-prep.md) 「PostgreSQL 확인」, 명령은 10절).

## 범위가 바뀜 (2026-11-13)

이 길로 Bronze까지 가는 것은 시세(`daily_prices`)와 환율(`fx_rates`)뿐입니다. 기사와 일정은 원천에 넣지 않고 `data/raw/`에서 화면 파일로 바로 갑니다([F 설계서](../ai/F-screen.md) 6절 6번, `loaders/schedule_events.py`). 관심도 적재기는 원천 테이블에 쓰지만 Bronze 작업(`infra/flink/bronze.sql`)은 다섯 테이블(주문, 체결, 잔고, 시세, 환율)만 읽어 Kafka에서 멈춥니다.
- 처음 1년치를 넣으면 시세 4,736개와 환율 245개의 `c` 이벤트가 한꺼번에 Kafka로 갑니다. Flink와 Bronze가 이 몰림을 받아 내는지 4회차에 봅니다.
- 적재기는 [`loaders/`](../../loaders/README.md)에 있습니다.
