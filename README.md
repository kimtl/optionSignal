# optionSignal

스캘핑용 **콜/풋 보드**입니다. tastytrade 실시간 호가를 서버가 받아 보드에 찍고, 보는 사람은 모두 같은 웹 화면을 봅니다.

CLI로 숫자를 뽑는 도구가 아닙니다. 한 사람이 웹 서버를 켜면 나머지는 브라우저만 엽니다.

## 같이 보기

```bash
python -m pip install -e ".[dev]"
python -m optionsignal
```

기본은 `0.0.0.0:8000`, DTE ≤ 1입니다. tastytrade 키가 있으면 `/NQ`를 5초마다, 없으면 Yahoo 지연 시세입니다.

- 서버 켠 사람: http://127.0.0.1:8000
- 같은 와이파이의 다른 사람: 화면 위쪽 **공유 URL** (이 컴퓨터의 LAN IP:8000)

인터넷으로 원격에서 보려면 Railway에 올리면 됩니다.

```bash
python -m optionsignal serve --interval 5 --max-dte 1 --host 0.0.0.0 --port 8000
```

## 시세 출처

정확한 실시간은 **tastytrade DXLink (dxFeed)** 입니다. 계정에 시세 권한이 있으면 NQ 선물옵션 호가·IV가 스트림으로 들어옵니다. 서버는 그 캐시를 기본 5초마다 보드에 찍습니다.

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
| `OPTIONSIGNAL_MAX_DTE=1` | 오늘·내일 만기만 |
| `OPTIONSIGNAL_SYMBOL=/NQ` | 나스닥 선물옵션 |
| `TASTYTRADE_CLIENT_SECRET` | tastytrade OAuth |
| `TASTYTRADE_REFRESH_TOKEN` | tastytrade refresh token |

Yahoo가 데이터센터 IP를 막으면 보드에 에러가 뜹니다. 키가 있으면 Yahoo를 쓰지 않습니다.

## 스캘핑에서 볼 숫자

ATM 콜−풋은 패리티 때문에 거의 안 움직입니다. 단타는 **1분 변화**를 봅니다.

| 숫자 | 의미 |
| --- | --- |
| **CPPI** | 근월 spot ±8% 옵션의 (콜−풋)/(콜+풋). 거래량×중간가 |
| **1분 Δ** | 직전 분 대비 CPPI 변화. 급등의 방향 |
| **1분 프리미엄 흐름** | 이번 분에 콜 프리미엄이 늘었는지, 풋이 늘었는지 |
| **5분 Δ** | 조금 더 굵은 흐름 |
| **25Δ RR** | 콜 IV − 풋 IV. 지수는 원래 풋 스큐라 음수가 흔함 |

정규장(뉴욕 09:30–16:00)과 CME 거의 24시간 선물은 tastytrade 스트림이 따라갑니다. 장후 Yahoo 폴링은 쓰지 마세요.

테스트:

```bash
python -m pytest -q
```
