# 데이터

원본 파일은 git에 올리지 않습니다(`data/raw/`, `data/generated/`는 `.gitignore`에 있습니다).

이 프로젝트의 도메인은 **가상의 모바일 증권 앱**입니다. 고객 주문, 체결, 잔고는 어디에도 공개되지 않는 데이터라 직접 만들고, 종목 가격만 실제 공공 데이터를 받습니다.

| 데이터 | 출처 | 라이선스와 조건 | 받는 방법 | 쓰는 곳 |
|---|---|---|---|---|
| 국내 주식 일별 시세(2종목) | 금융위원회_주식시세정보, [공공데이터포털](https://www.data.go.kr/data/15094808/openapi.do) | 공공누리 제4유형(출처표시, 상업적 이용금지, 변경금지). 제3자 재배포 금지. 거래일 다음 영업일 오후 1시 이후에 나옴 | 공공데이터포털에서 각자 인증키를 받아 `loaders/daily_prices.py --market kr` | `source.daily_prices` |
| 미국 상장 ETF와 개별 기업 일별 시세(17개) | Massive(옛 Polygon.io) 무료 요금제, 일별 집계 | 개인, 비업무, 비상업 용도만. 재배포 금지([약관](https://massive.com/legal/market-data-terms-of-service)). 분당 5번 | 각자 API 키를 받아 `loaders/daily_prices.py --market us --adjusted false`와 `true`(두 판 모두 받음. `false`는 원천 시세, `true`는 수정 종가로 화면 파일에) | `source.daily_prices` |
| 원/달러 환율 | 한국은행 ECOS Open API(`731Y001`, `0000001` 원/미국달러 매매기준율, 일별) | 무료 회원가입 뒤 인증키. ECOS에서 제공된 정보임을 표시(이용약관 제7조). 값은 저장소에 두지 않음. 한국 휴일에는 값이 없음 | 각자 인증키를 받아 `loaders/fx_rates.py` | `source.fx_rates`(`infra/postgres/init/20_fx_rates.sql`) |
| 코스피 일별 지수 | 한국은행 ECOS Open API(`802Y001`, `0001000`) | 무료 회원가입 뒤 인증키. ECOS에서 제공된 정보임을 표시(이용약관 제7조). 지수의 저작권은 통계 작성기관에 있으므로 값은 저장소에 두지 않음 | 각자 인증키를 받아 적재기 실행 | E03 판정(`docs/ai/experiments/E03-regime.md`) |
| 고객 주문, 체결, 잔고 | 직접 만든 합성 데이터 | 제한 없음 | 시나리오 SQL(`infra/postgres/scenario/`)과 규모 시험 생성기(`loaders/load_test.py`) | `source.orders`, `source.executions`, `source.holdings` |
| 미국 일정(FOMC 금리 결정, CPI와 고용 보고서 발표일) | [연준 FOMC 일정 페이지](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm), FRED API(`fred/release/dates`, release 10과 50) | FOMC 페이지는 미국 정부 저작물(공공 영역), 출처 표시. FRED는 무료 키, 「This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.」 표시, FRED를 대신하는 서비스 금지. 국내 일정(한국은행, 국가데이터처)은 E02 1판에서 받지 않음([`docs/ai/data-prep.md`](../docs/ai/data-prep.md) 6절) | `loaders/schedule_events.py`(FRED는 각자 키를 받아 `FRED_API_KEY`) → `data/raw/schedule/` | 화면 파일의 `events`(E02 근거, 원천 DB에 넣지 않음) |

- 받은 시세는 재배포할 수 없으므로 저장소에는 **받는 코드만** 둡니다(`loaders/`). 화면이나 글에 시세에서 나온 값을 보일 때는 출처를 적습니다.
- 적재기는 받은 원본을 `data/raw/`에 두고, 값이 바뀐 행만 고칩니다(같은 값으로 다시 넣어도 CDC 이벤트가 생기지 않음). 쓰는 법은 [`loaders/README.md`](../loaders/README.md), 2026-11-11에 실제로 받아 본 결과는 [`docs/ai/data-prep.md`](../docs/ai/data-prep.md).
- API 키는 `.env`(git 제외)에 둡니다. `.env.example`에는 이름만 적습니다.

## 종목

투자 판단이 아니라 데이터 설계 기준으로 고른 종목입니다. 국내 두 종목에 더해, 미국 쪽은 미국 시장과 세계 정세를 읽을 수 있도록 자산군마다 대표 ETF를 골랐습니다. 주가지수, 금리, 금, 유가, 환율은 그 자체로는 주식이 아니라서 ETF를 대리 지표로 씁니다.

| 묶음 | 코드 | 이름 | 통화 | 자산군 |
|---|---|---|---|---|
| 국내 | `005930` | 삼성전자 | KRW | 주식 |
| 국내 | `000660` | SK하이닉스 | KRW | 주식 |
| 미국 주식 | `SPY` | SPDR S&P 500 ETF | USD | 주식 |
| 미국 주식 | `QQQ` | Invesco QQQ(나스닥 100) | USD | 주식 |
| 미국 주식 | `IWM` | iShares 러셀 2000 ETF | USD | 주식 |
| 금리와 신용 | `SHY` | iShares 1 ~ 3년 미국 국채 ETF | USD | 채권 |
| 금리와 신용 | `TLT` | iShares 20년 이상 미국 국채 ETF | USD | 채권 |
| 금리와 신용 | `HYG` | iShares 하이일드 회사채 ETF | USD | 채권 |
| 원자재 | `GLD` | SPDR 금 ETF | USD | 원자재 |
| 원자재 | `USO` | 미국 원유 펀드 | USD | 원자재 |
| 통화 | `UUP` | Invesco 미국 달러 강세 ETF | USD | 통화 |
| 미국 밖 | `EFA` | iShares MSCI EAFE ETF | USD | 주식 |
| 미국 밖 | `EEM` | iShares MSCI 신흥국 ETF | USD | 주식 |
| 가상자산 | `IBIT` | iShares 비트코인 ETF | USD | 가상자산 |
| 미국 개별 기업 | `NVDA` | NVIDIA | USD | 주식 |
| 미국 개별 기업 | `AAPL` | Apple | USD | 주식 |
| 미국 개별 기업 | `TSLA` | Tesla | USD | 주식 |
| 미국 개별 기업 | `GOOGL` | Alphabet Class A | USD | 주식 |
| 미국 개별 기업 | `TSM` | TSMC ADR | USD | 주식 |

- 개별 기업 5개는 ETF에 표시되는 기사가 거의 없어 뉴스 근거를 위해 더했습니다(`docs/ai/experiments/E02-rag.md`). 시장을 읽는 지표에는 ETF만 씁니다.
- 수익률은 모두 가격 수익률입니다. 채권 ETF의 분배금은 반영하지 않습니다.
- `USO`와 `UUP`는 선물로 만든 ETF라 오래 보면 실제 유가, 달러 인덱스와 벌어집니다.

## 확장 예정: 관심도

핵심 파이프라인이 돈 뒤, 정해 둔 주제(금리, 관세, 유가 등)가 얼마나 관심을 받았는지를 날짜별로 모아 `source.attention_daily`에 넣을 계획입니다. 테이블은 `infra/postgres/init/30_attention_news.sql`, 주제 목록 초안은 `loaders/topics.csv`(확정 전)입니다.

| 데이터 | 출처 | 라이선스와 조건 | 적재기 |
|---|---|---|---|
| 주제어가 나온 기사 수, 비율, 어조 | [GDELT DOC 2.0 API](https://www.gdeltproject.org/about.html) | 이용과 재배포 허용, GDELT 출처 표기 필수. 5초에 1번까지. 최근 약 3개월만 찾음(더 오래된 날은 원본 파일이나 BigQuery 공개 데이터셋) | `loaders/attention_gdelt.py` |
| 문서별 하루 조회수 | 위키백과 조회수 API(Wikimedia REST API) | 키 없음. 프로그램 이름과 연락처를 담은 User-Agent 필요(연락처는 `.env`의 `WIKIMEDIA_CONTACT`) | `loaders/attention_wikipedia.py` |
| 종목별 뉴스 제목, 요약, 감성 | Massive 종목 뉴스 | 시세와 같은 약관(개인, 비상업, 재배포 금지). 기사 본문은 받지도 남기지도 않음(주소만 남기고 열지 않음). 요약은 제공처가 주는 짧은 글 | `loaders/news_massive.py` → `source.news_items`, `source.news_ticker_sentiment` |
| 한국 검색량의 상대값 | 네이버 데이터랩 검색어 트렌드 API | 네이버 오픈API 약관을 따름. 받은 값은 재배포하지 않음. 키가 `.env`에 있을 때만 씀 | `loaders/attention_naver.py` |

**관심도와 가격의 관계는 "같이 움직였다"(상관)까지만 말합니다. "그래서 움직였다"(인과)라고 하지 않습니다.** 인과 표현, 거꾸로일 가능성, 공통 원인, 우연, 추세끼리의 착시, 표본 크기를 결과마다 점검하고, 관심도 지표의 설명과 화면에 이 문구를 고정합니다.

## 합성 데이터의 가정

- 계좌와 고객은 가짜 번호(`A-0001` 같은)만 쓰고 이름이나 개인 정보는 만들지 않습니다.
- 한 계좌가 한 종목을 여러 번 나눠 사고팝니다. 주문 가격은 받은 실제 종가 근처에서 만듭니다. 국내 종목은 한국 장중에, 미국 종목은 미국 장중에 주문을 만듭니다.
- 수량은 정수 주식만 씁니다. 수수료, 세금, 예수금, 환전, 소수점 거래, 장 시간 밖 주문은 뺍니다.
- 정정과 취소는 실제 증권사처럼 새 주문 번호를 붙이지 않고, 주문 한 행을 수정하는 것으로 줄였습니다.
- 분포(하루 주문 수, 정정과 취소 비율)는 생성기를 만들 때 이 절에 적습니다.

## 규칙

- 공개 데이터나 직접 만든 데이터만 씁니다.
- 합성 데이터와 가정(분포, 비율, 변경 시나리오)은 이 파일에 적습니다.
- 받는 방법은 명령으로 남겨, 다른 사람이 같은 파일을 받을 수 있게 합니다.
