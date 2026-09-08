# optionSignal

스캘핑용 **콜/풋 보드**입니다. tastytrade 실시간 호가를 서버가 받아 보드에 찍고, 보는 사람은 모두 같은 웹 화면을 봅니다.

CLI로 숫자를 뽑는 도구가 아닙니다. 한 사람이 웹 서버를 켜면 나머지는 브라우저만 엽니다.

## 같이 보기

```bash
python -m pip install -e ".[dev]"
python -m optionsignal
```

기본은 `0.0.0.0:8000`, **0DTE만** 봅니다. tastytrade 키가 있으면 `/NQ` 0DTE를 5초마다, 없으면 Yahoo 지연 시세입니다.

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
| `OPTIONSIGNAL_OTM_POINTS=150` | CPPI에 넣을 옵션: 등가격에서 ±N포인트. 비우면 ±8% 밴드. 화면 「선정」에서도 바꿀 수 있음 |
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

## 스캘핑에서 볼 숫자

ATM 콜−풋은 패리티 때문에 거의 안 움직입니다. 단타는 **1분 변화**를 봅니다.

| 숫자 | 의미 |
| --- | --- |
| **CPPI** | 0DTE 콜·풋 프리미엄(중간가×거래량 합)의 (콜−풋)/(콜+풋). 옵션 선정은 「선정」 메뉴: 등가격 ±8% 밴드(기본) 또는 등가격 행사가에서 ±100/150/200/300포인트 |
| **1분 Δ** | 직전 분 대비 CPPI 변화. 급등의 방향 |
| **1분 프리미엄 흐름** | 이번 분에 콜 프리미엄이 늘었는지, 풋이 늘었는지 |
| **5분 Δ** | 조금 더 굵은 흐름 |
| **25Δ RR** | 콜 IV − 풋 IV. 지수는 원래 풋 스큐라 음수가 흔함 |

CPPI 카드 아래 「선정 행사가」에 지금 합산에 들어간 콜/풋 행사가 범위가 나옵니다. 차트는 파란 선이 CPPI(왼쪽 축), 노란 캔들이 NQ 선물(오른쪽 축)입니다.

## 옵션 체인 탭

「옵션 체인 · 0DTE」 탭은 CPPI를 만드는 원재료를 그대로 보여 줍니다. 행사가별로 콜(델타·OI·거래량·IV·매수·매도·체결) | 행사가 | 풋(체결·매수·매도·IV·거래량·OI·델타). 등가격 행은 노란 테두리, 거래량 상위 10%는 노란 글씨, 내가격은 살짝 밝은 배경입니다.

- 델타는 tastytrade DXLink 그릭스 값입니다. 그릭스가 아직 안 왔거나 Yahoo 폴백이면 IV로 계산한 모델값에 `∗`가 붙습니다.
- 거래량은 DXLink `Trade.day_volume`, 없으면 REST `volume`. OI는 체인 로딩 시 REST 값.
- 「행사가 범위」로 현재가 ±200/400/800포 또는 전체를 고르고, 「등가격으로」가 표를 다시 등가격에 맞춥니다.
- `GET /api/chain` 이 같은 데이터를 JSON으로 줍니다. 보드가 찍을 때마다(기본 5초) 탭이 열려 있으면 자동 갱신됩니다.

정규장(뉴욕 09:30–16:00)과 CME 거의 24시간 선물은 tastytrade 스트림이 따라갑니다. 장후 Yahoo 폴링은 쓰지 마세요.

테스트:

```bash
python -m pytest -q
```
