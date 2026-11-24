# 적재기

공공 데이터와 무료 API에서 받은 값을 원천 PostgreSQL의 `source` 스키마에 넣는 프로그램입니다. 여기 넣은 행은 주문, 체결처럼 CDC로 Kafka까지 갑니다(`cdc_pub`이 `source` 스키마 전체를 담음).

- 받은 원본은 `data/raw/`(git 제외)에만 둡니다. 시세, 뉴스, 관심도 값은 재배포하지 않습니다. 출처와 조건은 [`data/README.md`](../data/README.md).
- 키와 연락처는 `.env`에서 읽고 출력하지 않습니다. 같은 이름의 환경 변수가 있으면 그 값이 먼저입니다.
- TLS는 운영체제 인증서 저장소로 검사합니다(`truststore`). 검사를 끄는 옵션은 없습니다.
- 모두 **값이 바뀐 행만 고칩니다**. 같은 값으로 다시 `UPDATE`해도 PostgreSQL은 새 행을 써서 `u` 이벤트가 생기므로, `ON CONFLICT ... DO UPDATE ... WHERE (...) IS DISTINCT FROM (...)`로 막습니다.

## 준비

```bash
python3 -m venv .venv
.venv/bin/pip install -r loaders/requirements.txt
```

테이블은 `infra/postgres/init/`의 `10_brokerage.sql`, `20_fx_rates.sql`, `30_attention_news.sql`입니다. 빈 볼륨으로 처음 띄우면 저절로 만들어지고, 이미 떠 있으면 `psql`로 직접 실행합니다.

## 적재기

| 파일 | 넣는 곳 | 출처 | 키(.env) | 기본 기간 |
|---|---|---|---|---|
| `daily_prices.py` | `source.daily_prices` | 국내: 공공데이터포털 금융위원회_주식시세정보, 미국: Massive 일별 집계 | `DATA_GO_KR_SERVICE_KEY`, `MASSIVE_API_KEY` | 최근 7일 |
| `fx_rates.py` | `source.fx_rates` | 한국은행 ECOS 731Y001 원/미국달러 매매기준율 | `ECOS_API_KEY` | 최근 7일 |
| `news_massive.py` | `source.news_items`, `source.news_ticker_sentiment` | Massive 종목 뉴스(제목, 요약, 종목 감성) | `MASSIVE_API_KEY` | 최근 2일(UTC) |
| `attention_wikipedia.py` | `source.attention_daily` | Wikimedia 조회수 API(영어 위키백과, 사람 조회) | `WIKIMEDIA_CONTACT` | 최근 7일(UTC) |
| `attention_gdelt.py` | `source.attention_daily` | GDELT DOC 2.0 API(최근 약 3개월만) | 없음 | 최근 7일(UTC) |
| `attention_naver.py` | `source.attention_daily` | 네이버 데이터랩 검색어 트렌드 | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`(없으면 건너뜀) | 고정 시작일 ~ 어제 |

공통 옵션: `--start`, `--end`(YYYY-MM-DD, 그날 포함), `--days`(끝일부터 거꾸로 며칠), `--dry-run`(받아서 원본만 저장하고 DB에 쓰지 않음), `--from-raw`(API를 부르지 않고 저장된 원본으로 적재). 접속값은 `.env`의 `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT`(기본 5432)와 `POSTGRES_HOST`(기본 localhost)입니다.

```bash
# 처음 1년치
.venv/bin/python loaders/daily_prices.py --days 365 --adjusted false
.venv/bin/python loaders/fx_rates.py --days 365

# 그 뒤 매일(최근 며칠을 다시 받아 바뀐 행만 고침)
.venv/bin/python loaders/daily_prices.py --adjusted false
.venv/bin/python loaders/fx_rates.py
.venv/bin/python loaders/news_massive.py
```

- **수정주가(`--adjusted`)는 2026-11-13에 정했습니다.** 두 판을 모두 받아 `false`는 원천 시세로, `true`(수정 종가)는 화면 파일로 바로 갑니다([F 설계서](../docs/ai/F-screen.md) 6절). 원천 테이블에는 두 판을 섞지 않습니다(선택지는 [`docs/ai/data-prep.md`](../docs/ai/data-prep.md)).
- 미국 시세는 평일 5일 넘는 기간이면 종목마다 기간 조회(17번), 그 안이면 날짜마다 전 종목 조회(하루 1번)를 씁니다(`--mode`로 고를 수 있음). 둘이 낸 값은 같았습니다(2026-11-09, 11-10 대조).
- Massive 무료 요금제는 분당 5번이라 호출 사이에 12.5초 쉽니다. 시세와 뉴스 적재기를 동시에 돌리지 않습니다.
- 국내 시세는 거래일 다음 영업일 오후 1시 이후, 미국 시세는 미국 장 마감 뒤, ECOS 환율은 고시한 날에 나옵니다.
- Massive 거래량은 소수가 섞여 와서(소수점 거래) 정수로 반올림해 넣고, 반올림한 행 수를 알려 줍니다.

## 화면 데이터 기간 늘리기

화면 데이터 기간은 받은 원본 구간 목록(`data/generated/window.json`, git 밖)을 이어 붙인 것입니다. 목록이 없으면 첫 구간(평가 기준 2년치)만 씁니다([결정 기록 0015](../docs/decisions/0015-extend-data-window-keep-eval-frozen.md)). 받기는 Mac이, 받은 원본의 처리는 Airflow DAG `window_extend`가 합니다([결정 기록 0016](../docs/decisions/0016-window-extend-fetch-on-mac-process-in-airflow.md)).

| 순서 | 어디서 | 하는 일 |
|---|---|---|
| `make window-fetch` | Mac(키, 인터넷) | 국내 시세가 나온 마지막 거래일을 묻고, 구간 끝부터 그날까지 원본 38개를 `data/raw/window/`에 받은 뒤 `next.json`(구간과 지문)을 적음 |
| `make window-load` | Airflow | 받아 둔 것이 있으면 `window_extend`를 한 번: 지문과 겹치는 날 확인, 원천 적재, Bronze 기다림, 구간 더하기, `lakehouse_refresh`, 화면 확인 |
| `make window-extend` | 둘 다 | 위 둘을 차례로 |

- 하루 겹치는 까닭: 미국 종가는 미국 장 마감 뒤(서울 아침)에 나와, 받을 때 끝날의 종가가 아직 없을 수 있습니다. 겹친 날은 원본끼리 값이 같아야 합니다.
- 끝날은 「어제」가 아니라 국내 시세가 나온 마지막 거래일입니다. 국내 시세는 다음 영업일 오후 1시 뒤에 나와, 주말에 어제를 끝날로 받으면 금요일 국내 시세가 빠집니다.
- 받기가 Mac인 까닭: HTTPS를 검사하는 프록시가 있는 네트워크에서는 컨테이너가 API의 인증서를 믿지 못할 수 있고, 키는 `.env`에만 둡니다. 컨테이너는 `data/raw/`를 읽기만 합니다.
- 적재기의 원본 폴더는 환경 변수 `LOADER_RAW_DIR`(기본 `data/raw`)로 바꿉니다. 늘린 구간은 `data/raw/window`입니다.
- 매일 받기: 평일 14:30에 Mac의 launchd가 [`scripts/window_fetch_daily.sh`](../scripts/window_fetch_daily.sh)를 부릅니다(`~/Library/LaunchAgents`에 plist를 두고 `launchctl bootstrap gui/$(id -u) <plist>`). 기록은 `data/generated/logs/window_fetch.log`, 실패하면 macOS 알림입니다. 받기는 늘 구간 끝부터 받으므로 레이크가 꺼져 있던 날도 빠지지 않습니다.

## 관심도

주제와 출처별 검색어는 [`topics.csv`](topics.csv)입니다(주제, GDELT 검색어, 위키 문서, 네이버 키워드, 관련 종목). 지금 목록은 설계 초안의 예 일곱 개이고 **확정하지 않았습니다**. 관련 종목 칸은 관심도와 시세를 잇는 다리이고, 볼 조합을 미리 정해 우연히 높은 상관을 고르지 않게 하는 장치입니다.

관심도와 가격은 "같이 움직였다"(상관)까지만 말합니다. 결과마다 인과 표현, 거꾸로일 가능성, 공통 원인, 우연, 추세끼리의 착시, 표본 크기를 점검합니다(`data/README.md`).

| 출처 | 날짜 기준 | 지난 값이 바뀌나 | 주의 |
|---|---|---|---|
| 위키백과 | UTC | 거의 안 바뀜 | User-Agent에 프로그램 이름과 연락처. `WIKIMEDIA_CONTACT`는 공개 저장소에 적어도 되는 메일이나 웹 주소 |
| GDELT | UTC | 늦은 색인으로 최근 며칠이 늘 수 있음 | 5초에 1번. 429면 10, 20, 40초 기다렸다 다시 하고, 두 주제가 연달아 막히면 멈춤 |
| 네이버 | 한국 | 요청 기간의 최댓값이 100이라 끝일이 늘면 바뀔 수 있음 | 주제마다 따로 요청하고 시작일을 고정(`--base-start`, 기본 2025-01-01)한 뒤 매번 전체를 다시 넣음 |

## 종목

[`instruments.csv`](instruments.csv)는 종목 19개의 이름, 시장, 통화, 자산군, 묶음, 시세 출처입니다. dbt seed로 옮겼습니다(`dbt/seeds/instruments.csv`가 이 파일을 가리키는 링크라, 링크를 따르지 않는 환경에서는 복사본이 필요합니다).
