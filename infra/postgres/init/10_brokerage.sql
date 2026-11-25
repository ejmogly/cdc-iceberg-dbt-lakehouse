-- 가상의 모바일 증권 앱 원천 테이블(3회차 설계, 2026-11-11 확정).
-- 데이터 볼륨이 비어 있을 때(최초 기동) 00_cdc_setup.sh 다음에 자동으로 실행됩니다.
-- 이미 떠 있는 환경에서는 직접 실행합니다(학습 가이드 03 11-9).
-- 가격과 환율은 numeric이라, 커넥터에 decimal.handling.mode=string을 먼저 넣어 둡니다.

-- 주문 한 건. 정정은 새 주문 번호 대신 이 행을 고칩니다.
-- order_type TRANSFER_IN(입고): 다른 곳에서 들여온 주식. 매수 쪽(side BUY)이고 체결가는 취득 단가입니다.
-- 화면의 지금 잔고 줄(데이터 기간 전부터 가진 주식)을 데이터 첫날의 보유로 넣을 때 씁니다(docs/decisions/0013,
-- 2026-11-19 더함). 이미 떠 있는 DB에는 infra/postgres/migrate/0013_orders_transfer_in.sql로 넣습니다.
CREATE TABLE source.orders (
  order_id    bigint PRIMARY KEY,
  account_id  text   NOT NULL,
  symbol      text   NOT NULL,
  side        text   NOT NULL
    CHECK (side IN ('BUY', 'SELL')),
  order_type  text   NOT NULL
    CONSTRAINT orders_order_type_check
    CHECK (order_type IN ('LIMIT', 'MARKET', 'TRANSFER_IN')),
  order_qty   int    NOT NULL
    CHECK (order_qty > 0),
  limit_price numeric(18,4),
  filled_qty  int    NOT NULL DEFAULT 0,
  status      text   NOT NULL
    CHECK (status IN ('ACCEPTED', 'PARTIAL',
      'FILLED', 'CANCELLED', 'REJECTED')),
  ordered_at  timestamptz NOT NULL,
  updated_at  timestamptz NOT NULL,
  CHECK (filled_qty BETWEEN 0 AND order_qty),
  CHECK ((order_type = 'LIMIT')
         = (limit_price IS NOT NULL))
);

-- 체결 한 건. 한 주문이 여러 번 나눠 체결될 수 있습니다.
-- fx_rate: 미국 종목 체결 때 적용한 원/달러 환율. 국내 종목은 비워 둡니다.
CREATE TABLE source.executions (
  execution_id bigint PRIMARY KEY,
  order_id     bigint NOT NULL
    REFERENCES source.orders (order_id),
  exec_qty     int    NOT NULL
    CHECK (exec_qty > 0),
  exec_price   numeric(18,4) NOT NULL
    CHECK (exec_price > 0),
  fx_rate      numeric(12,4)
    CHECK (fx_rate > 0),
  executed_at  timestamptz NOT NULL,
  updated_at   timestamptz NOT NULL
);

-- 체결이 철회(삭제)되면 그 체결이 어느 주문의 것이었는지(order_id)를 알아야
-- 영향받는 마트 키를 찾을 수 있습니다. 기본값(DEFAULT)이면 삭제 이벤트에 키만 오므로
-- 이 테이블만 지워지기 전 행 전체를 남기게 합니다(가이드 06 14절, 2026-11-11 정함).
ALTER TABLE source.executions REPLICA IDENTITY FULL;

-- 외래 키를 거는 쪽 칸(order_id)에도 인덱스를 둡니다. 없으면 주문 한 건을 지울 때마다
-- 그 주문을 가리키는 체결이 있는지 체결 테이블을 처음부터 훑습니다. 규모 시험에서 5만 건 지우기가
-- 36.6초 걸렸고 이 인덱스를 두면 0.62초였습니다(docs/decisions/0012, 2026-11-18 정함).
-- 이미 떠 있는 DB에는 infra/postgres/migrate/0012_executions_order_id_index.sql로 넣습니다(make source-migrate).
CREATE INDEX executions_order_id_idx ON source.executions (order_id);

-- 계좌별 종목 잔고의 현재 상태. 전량 매도하면 행을 지웁니다.
CREATE TABLE source.holdings (
  account_id text NOT NULL,
  symbol     text NOT NULL,
  qty        int  NOT NULL CHECK (qty > 0),
  avg_price  numeric(18,4) NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (account_id, symbol)
);

-- 종목의 하루 시세. 공공 데이터를 받아 넣습니다(provider: data.go.kr 또는 massive).
CREATE TABLE source.daily_prices (
  symbol      text NOT NULL,
  trade_date  date NOT NULL,
  open_price  numeric(18,4) NOT NULL,
  high_price  numeric(18,4) NOT NULL,
  low_price   numeric(18,4) NOT NULL,
  close_price numeric(18,4) NOT NULL,
  volume      bigint NOT NULL,
  provider    text NOT NULL,
  loaded_at   timestamptz NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);
