# optionSignal

스캘핑용 **1분 콜/풋 보드**입니다. 서버가 1분마다 나스닥(QQQ/NDX) 근월 옵션 체인을 한 번 찍고, 보는 사람은 모두 같은 웹 화면을 봅니다.

CLI로 숫자를 뽑는 도구가 아닙니다. 한 사람이 웹 서버를 켜면 나머지는 브라우저만 엽니다.

## 같이 보기

```bash
python -m pip install -e ".[dev]"
python -m optionsignal
```

기본은 `0.0.0.0:8000`, 1분 간격, DTE ≤ 1 (오늘·내일 만기)입니다.

- 서버 켠 사람: http://127.0.0.1:8000
- 같은 와이파이의 다른 사람: 화면 위쪽 **공유 URL** (이 컴퓨터의 LAN IP:8000)

인터넷으로 원격에서 보려면 Railway에 올리면 됩니다. 브라우저마다 Yahoo를 치지 않고, 서버 분봉 하나만 구독합니다.

```bash
python -m optionsignal serve --interval 60 --max-dte 1 --host 0.0.0.0 --port 8000
```

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
| `OPTIONSIGNAL_INTERVAL=60` | 분 간격 |
| `OPTIONSIGNAL_MAX_DTE=1` | 오늘·내일 만기만 |
| `OPTIONSIGNAL_SYMBOL=QQQ` | 기본 심볼 |

Yahoo가 데이터센터 IP를 막으면 보드에 에러가 뜹니다. 그때는 로그를 보면 됩니다. 정규장에 가장 잘 움직입니다.

## 스캘핑에서 볼 숫자

ATM 콜−풋은 패리티 때문에 거의 안 움직입니다. 단타는 **1분 변화**를 봅니다.

| 숫자 | 의미 |
| --- | --- |
| **CPPI** | 근월 spot ±8% 옵션의 (콜−풋)/(콜+풋). 거래량×중간가 |
| **1분 Δ** | 직전 분 대비 CPPI 변화. 급등의 방향 |
| **1분 프리미엄 흐름** | 이번 분에 콜 프리미엄이 늘었는지, 풋이 늘었는지 |
| **5분 Δ** | 조금 더 굵은 흐름 |
| **25Δ RR** | 콜 IV − 풋 IV. 지수는 원래 풋 스큐라 음수가 흔함 |

정규장(뉴욕 09:30–16:00)에 Yahoo 옵션 호가가 가장 믿을 만합니다. 장전/장후/주말은 지연될 수 있습니다. 실제 NQ 선물옵션 호가는 CME 유료라 같은 지수인 QQQ/NDX로 봅니다.

테스트:

```bash
python -m pytest -q
```
