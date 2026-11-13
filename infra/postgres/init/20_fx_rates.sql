-- 원/달러 같은 환율의 하루 값(AI 트랙 손익의 원화 환산, 2026-11-11 초안).
-- 데이터 볼륨이 비어 있을 때(최초 기동) 10_brokerage.sql 다음에 자동으로 실행됩니다.
-- 이미 떠 있는 환경에서는 직접 실행합니다. source 스키마라 cdc_pub에 저절로 들어갑니다.
-- 적재기: loaders/fx_rates.py(한국은행 ECOS 731Y001, 매매기준율).

-- 통화쌍 하나의 고시일 하루 값. 한국 휴일에는 행이 없습니다.
-- currency_pair: 'USDKRW'처럼 기준 통화 + 표시 통화. rate는 기준 통화 1단위의 표시 통화 값.
-- rate_date: 고시한 나라의 날짜(ECOS면 한국 날짜). 미국 거래일과 맞추는 규칙은 Gold에서 정합니다.
-- rate는 executions.fx_rate와 같은 자료형입니다.
CREATE TABLE source.fx_rates (
  currency_pair text NOT NULL,
  rate_date     date NOT NULL,
  rate          numeric(12,4) NOT NULL
    CHECK (rate > 0),
  provider      text NOT NULL,
  loaded_at     timestamptz NOT NULL,
  PRIMARY KEY (currency_pair, rate_date)
);
