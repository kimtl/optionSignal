# optionSignal

스캘핑용 **NQ 선물 · 0DTE 옵션 시세 보드**입니다. tastytrade 실시간 호가를 서버가 받아 보드에 찍고, 보는 사람은 모두 같은 웹 화면을 봅니다.

- **보드** 탭: NQ 선물 봉차트(1분/5분봉, 최대 12시간)에 원하는 0DTE 옵션을 골라 시세 선을 겹쳐 봅니다.
- **옵션 체인** 탭: 행사가별 콜/풋 호가·거래량·OI·델타.

CLI로 숫자를 뽑는 도구가 아닙니다. 한 사람이 웹 서버를 켜면 나머지는 브라우저만 엽니다.

## 같이 보기

```bash
python -m pip install -e ".[dev]"
python -m optionsignal
```

기본은 `0.0.0.0:8000`, **0DTE만** 봅니다. tastytrade 키가 있으면 `/NQ` 0DTE를 5초마다, 없으면 Yahoo 지연 시세입니다.

**만기 넘어가기**: NQ 옵션(과 QQQ/NDX)은 동부시간 **오후 4시**에 만기입니다. 그 시각이 지나면 오늘 만기는 버리고 **다음 만기**(보통 내일, 금요일 저녁이면 월요일)를 0DTE로 취급합니다. tastytrade 피드는 4시에 체인을 다시 불러 새 계약을 구독하고, 새 만기 계약의 지난 12시간도 다시 백필합니다. 보드의 「옵션 선택」 라벨과 체인 탭 상단에 지금 쓰는 만기(`만기 09-10 0DTE`)가 표시되고, 넘어간 상태면 안내 문구가 붙습니다. 옵션 시세 이력(`option_ticks`)은 만기별로 저장되어 어제 24700C와 오늘 24700C가 섞이지 않습니다. `/api/live`의 `status.expiry`·`status.session_date`, 틱의 `headline_expiry`·`headline_dte`·`session_date`로 확인할 수 있습니다.

- 서버 켠 사람: http://127.0.0.1:8000
- 같은 와이파이의 다른 사람: 화면 위쪽 **공유 URL** (이 컴퓨터의 LAN IP:8000)

인터넷으로 원격에서 보려면 Railway에 올리면 됩니다.

```bash
python -m optionsignal serve --interval 5 --max-dte 0 --host 0.0.0.0 --port 8000
```

## 시세 출처

정확한 실시간은 **tastytrade DXLink (dxFeed)** 입니다. 계정에 시세 권한이 있으면 **NQ 0DTE** 호가·IV가 스트림으로 들어옵니다.

Yahoo는 키가 없을 때만 쓰는 지연 시세입니다. 실시간이 아닙니다.

### tastytrade 연결

1. tastytrade에서 [OAuth 앱](https://developer.tastytrade.com/)을 만들고 client secret을 저장합니다.
2. 같은 화면에서 **Create Grant**로 refresh token을 만듭니다. (만료되지 않습니다.)
3. 계정이 **NQ 선물옵션 실시간 시세** 권한을 갖고 있는지 확인합니다.
4. 환경 변수:

| 변수 | 값 |
| --- | --- |
| `TASTYTRADE_CLIENT_SECRET` | OAuth client secret |
| `TASTYTRADE_REFRESH_TOKEN` | refresh token |
| `TASTYTRADE_IS_TEST` | 샌드박스는 `true` (실전 시세는 `false`) |
| `OPTIONSIGNAL_SYMBOL` | `/NQ` |
| `OPTIONSIGNAL_INTERVAL` | `5` |

키가 있으면 보드가 자동으로 `/NQ` 실시간으로 뜹니다. Railway Variables에도 똑같이 넣으면 됩니다.

화면이 **tastytrade 연결 중**에서 멈추면, 예전 버전은 `/NQ`를 선물 계약처럼 조회해서 기초가격이 안 잡힌 채 DXLink만 기다리고 있었습니다. 지금은 최근월물(`/NQU6` 같은 심볼)을 찾고, REST 호가로 보드를 먼저 찍은 뒤 DXLink를 붙입니다. 배포 후 `/health`의 `phase`, `contracts`, `quoted`, `error`를 보면 됩니다.

## Railway에 올리기

가능합니다. 이 저장소를 Railway 서비스 하나에 붙이면 됩니다. 인스턴스는 **1개**만 쓰세요. 복제본을 늘리면 분봉을 두 번 찍고 화면이 갈라집니다.

1. [Railway](https://railway.app) → **New Project** → **GitHub repo** 에서 `optionSignal` 선택
2. 빌드가 끝나면 **Settings → Networking → Generate Domain**
3. 나온 `https://….up.railway.app` 을 같이 보면 됩니다

시작 명령은 `railway.json`에 이미 있습니다. Railway가 `PORT`를 넣고, 앱은 `0.0.0.0`에 붙습니다.

선택:

| 변수 / 볼륨 | 용도 |
| --- | --- |
| Volume을 `/data`에 마운트 | 재배포해도 분봉 테이프가 남음. 없으면 재시작 때 차트는 초기화 |
| `OPTIONSIGNAL_INTERVAL=5` | tastytrade일 때 보드 찍는 간격(초) |
| `OPTIONSIGNAL_MAX_DTE=0` | 0DTE만 |
| `OPTIONSIGNAL_SYMBOL=/NQ` | 나스닥 선물옵션 |
| `OPTIONSIGNAL_OTM_POINTS=150` | 서버 기본 CPPI(headline) 선정: 등가격에서 ±N포인트. 비우면 ±3% 밴드. 화면의 「선정」은 이 값과 무관하게 브라우저별로 고릅니다 |
| `TASTYTRADE_CLIENT_SECRET` | tastytrade OAuth |
| `TASTYTRADE_REFRESH_TOKEN` | tastytrade refresh token |

Yahoo가 데이터센터 IP를 막으면 보드에 에러가 뜹니다. 키가 있으면 Yahoo를 쓰지 않습니다.

## `/api/tick` 502

Railway 로그의 `POST /api/tick 502 Bad Gateway` 는 게이트웨이가 죽은 게 아니라 **시세 조회가 실패한 것**입니다. 「지금 찍기」는 선택입니다. 서버가 이미 `OPTIONSIGNAL_INTERVAL`마다 찍습니다.

화면 위 요약 칸과 `/health`의 `error`에 이유가 뜹니다.

| 상황 | HTTP | 할 일 |
| --- | --- | --- |
| `/NQ`인데 tasty 키가 없음 | 400 | Variables에 `TASTYTRADE_CLIENT_SECRET` + `TASTYTRADE_REFRESH_TOKEN` 을 넣거나 심볼을 QQQ |
| tasty DXLink 연결 중 | 503 | 몇 초 기다리면 보드가 자동으로 찍힘 |
| 키/권한/0DTE 없음, Yahoo 차단 | 502 | `TASTYTRADE_IS_TEST=false` 인지, NQ 선물옵션 실시간 권한이 있는지, 오늘 만기가 있는지 확인 |

샌드박스(`TASTYTRADE_IS_TEST=true`)는 실전 호가가 아닙니다.

## 보드 탭

- **NQ 선물 봉차트**: 서버가 찍는 시세(기본 5초)를 1분봉/5분봉으로 묶어 초록/빨강 캔들로 그립니다. 30분~12시간까지 버튼·슬라이더·휠로 확대/축소, X축은 HH:MM, 마지막 가격은 노란 점선.
- **옵션 선택**: 행사가(등가격은 `· ATM`, 나머지는 현재가 대비 거리)와 콜/풋을 골라 「추가」하거나 「ATM 콜+풋」으로 한 번에. 최대 6개. 선택한 옵션은 오른쪽 축에 색깔 선(중간가)으로 겹쳐지고, 위 표에 중간가·매수·매도·체결·거래량·Δ1m·30분 Δ가 나옵니다. 선택은 브라우저(localStorage)에 남습니다.
- 옵션 시세 이력은 서버가 매 틱마다 0DTE 만기 전 계약을 `option_ticks` 테이블에 저장하므로, 나중에 고른 옵션도 지난 12시간 선이 바로 보입니다. `GET /api/option_series?keys=24700C,24650P&tf=1`.
- **화면별 설정**: 옵션 선택, CPPI 「선정」(모든 외가격 / 등가격 ±100·150·200·300포), CPPI 선 표시, 분봉·시간 범위는 모두 브라우저(localStorage)에만 저장되어 다른 사람의 조작에 영향받지 않습니다. 서버는 틱마다 모든 선정 규칙의 CPPI를 함께 계산해(`cppi_variants`) 내려 주고, 화면이 자기 선정값으로 카드·테이프·선을 그립니다. **지수 선택(NQ/ES/YM)만 서버 공통**입니다(피드가 하나라서). 모든 규칙은 **외가격만** 봅니다(등가격 행사가와 내가격은 콜·풋 모두 제외). 「±N포」는 콜 현재가 초과~현재가+N, 풋 현재가−N~현재가 미만으로 서로 거울상 범위입니다. 예: 지수 100, ±20이면 콜 101~120, 풋 80~99. 「모든 외가격」은 서버가 구독한 0DTE 체인(등가격 ±8%, 최대 250개 행사가) 안의 외가격 콜·풋 전부를 합산합니다.
- **CPPI 선**: 선물 봉차트 안에 선택한 선정 규칙의 CPPI가 하늘색 선(−1~+1 고정 스케일, 점선이 0)으로 겹쳐집니다. 차트 헤더의 「CPPI 선」 버튼으로 끄고 켭니다.
- **콜/풋 프리미엄 비율 차트**: 선물 차트 바로 아래 봉차트. 화면에서 선택한 옵션만 놓고 `콜 Σ(중간가×거래량) ÷ 풋 Σ(중간가×거래량) × 100`을 틱마다 계산해 1분/5분봉으로 묶습니다(100 = 콜·풋 프리미엄 흐름이 같음, 노란 점선). 콜과 풋을 최소 하나씩 골라야 하고, 거래량은 일중 누적이라 서버 실시간 틱만 씁니다(백필 캔들 제외). `GET /api/premium_ratio?calls=24700C,24750C&puts=24700P&tf=1`.
- **지수 선택**: 상단 드롭다운에서 NQ(나스닥)·ES(S&P 500)·YM(다우) 선물을 고릅니다. 바꾸면 서버가 tastytrade 피드를 그 상품으로 다시 붙이고(체인·백필 다시), 옵션 선택 목록은 상품별로 따로 저장됩니다. 셋 다 tastytrade 키가 필요합니다. `±100포` 같은 포인트 기준은 지수마다 스케일이 달라 ES/YM에서는 의미가 달라지니 참고하세요.
- **과거 시세 백필**: 서버가 켜지면 지난 12시간을 한 번 채워 넣습니다. tastytrade면 dxFeed 1분 캔들로 NQ 선물과 스트리밍 중인 0DTE 계약 전부를, 아니면 Yahoo 1분 캔들로 NQ=F만 가져옵니다(지연·휴장 시간대는 비어 있을 수 있음). 채워진 양은 차트 아래 힌트(`과거 N분 불러옴`)와 `/api/live`의 `status.backfill`에 나옵니다. 재배포로 DB가 비어도 차트는 페이지를 연 시점이 아니라 12시간 전부터 보입니다.
- **CPPI**(콜·풋 프리미엄 불균형)와 「선정」 메뉴, 1분 변화, RR, 테이프는 참고용으로 접힌 섹션에 남겨 두었습니다.

## 옵션 체인 탭

「옵션 체인 · 0DTE」 탭은 행사가별 콜/풋 시세를 그대로 보여 줍니다. 행사가별로 콜(델타·OI·거래량·IV·매수·매도·체결) | 행사가 | 풋(체결·매수·매도·IV·거래량·OI·델타). 등가격 행은 노란 테두리, 거래량 상위 10%는 노란 글씨, 내가격은 살짝 밝은 배경입니다.

- 델타는 tastytrade DXLink 그릭스 값입니다. 그릭스가 아직 안 왔거나 Yahoo 폴백이면 IV로 계산한 모델값에 `∗`가 붙습니다.
- 거래량은 DXLink `Trade.day_volume`, 없으면 REST `volume`. OI는 체인 로딩 시 REST 값.
- 「행사가 범위」로 현재가 ±200/400/800포 또는 전체를 고르고, 「등가격으로」가 표를 다시 등가격에 맞춥니다.
- `GET /api/chain` 이 같은 데이터를 JSON으로 줍니다. 보드가 찍을 때마다(기본 5초) 탭이 열려 있으면 자동 갱신됩니다.

정규장(뉴욕 09:30–16:00)과 CME 거의 24시간 선물은 tastytrade 스트림이 따라갑니다. 장후 Yahoo 폴링은 쓰지 마세요.

테스트:

```bash
python -m pytest -q
```
