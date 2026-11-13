-- 관심도와 종목 뉴스(확장 계획, 2026-11-11 초안). 설계는 data/README.md의 「확장 예정: 관심도」.
-- 데이터 볼륨이 비어 있을 때(최초 기동) 20_fx_rates.sql 다음에 자동으로 실행됩니다.
-- source 스키마라 cdc_pub에 저절로 들어갑니다(CDC로 받지 않으려면 다른 스키마로 옮깁니다).
-- 적재기: loaders/attention_*.py, loaders/news_massive.py.

-- 주제 하나의 하루 관심도 숫자 하나. 출처마다 지표가 달라 세로로 긴 모양입니다.
-- topic: loaders/topics.csv의 topic_id. obs_date: 출처의 날짜(GDELT, 위키백과는 UTC, 네이버는 한국 날짜).
-- provider: gdelt, wikipedia, naver. metric: article_count, article_share(%), tone, pageviews, search_index.
-- 지난 날짜 값이 바뀔 수 있습니다(GDELT 늦은 색인, 네이버 상대값). 적재기는 바뀐 행만 고칩니다.
CREATE TABLE source.attention_daily (
  topic     text NOT NULL,
  obs_date  date NOT NULL,
  provider  text NOT NULL,
  metric    text NOT NULL,
  value     numeric(18,4) NOT NULL,
  loaded_at timestamptz NOT NULL,
  PRIMARY KEY (topic, obs_date,
               provider, metric)
);

-- 기사 하나(Massive 종목 뉴스). 본문은 받지도 남기지도 않습니다.
-- description은 제공처가 주는 짧은 요약입니다. 받은 글은 재배포하지 않습니다.
CREATE TABLE source.news_items (
  article_id   text PRIMARY KEY,
  published_at timestamptz NOT NULL,
  publisher    text,
  title        text NOT NULL,
  description  text,
  article_url  text,
  tickers      text[] NOT NULL DEFAULT '{}',
  keywords     text[] NOT NULL DEFAULT '{}',
  provider     text NOT NULL,
  loaded_at    timestamptz NOT NULL
);

-- 기사와 종목마다 제공처가 붙인 감성과 이유 한 줄(Massive insights).
-- sentiment: positive, neutral, negative, 드물게 mixed(값을 막지 않음). 감성은 제공처의 판단이고 우리 판정이 아닙니다.
CREATE TABLE source.news_ticker_sentiment (
  article_id          text NOT NULL
    REFERENCES source.news_items (article_id),
  ticker              text NOT NULL,
  sentiment           text NOT NULL,
  sentiment_reasoning text,
  provider            text NOT NULL,
  loaded_at           timestamptz NOT NULL,
  PRIMARY KEY (article_id, ticker)
);
