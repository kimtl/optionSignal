from __future__ import annotations

from fastapi.testclient import TestClient

from optionsignal.models import OptionChain
from optionsignal.web import create_app
from tests.test_metrics import NOW, quote


def _chain(call_vol: int = 80, put_vol: int = 40) -> OptionChain:
    quotes = []
    for strike in (96, 98, 100, 102, 104):
        quotes.append(quote("call", strike, 1.2, volume=call_vol, iv=0.22, pct=8))
        quotes.append(quote("put", strike, 1.0, volume=put_vol, iv=0.24, pct=2))
    return OptionChain(
        symbol="QQQ",
        spot=100.0,
        futures_symbol="NQ=F",
        futures_price=20000.0,
        asof=NOW,
        quotes=quotes,
        source="test",
    )


def test_shared_minute_board(monkeypatch, tmp_path):
    state = {"call": 80, "put": 40}
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.setattr(
        "optionsignal.collector.fetch_chain",
        lambda **kwargs: _chain(state["call"], state["put"]),
    )
    app = create_app(start_collector=False, interval=60)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "LIVE" in home.text
        assert "1분봉" in home.text
        assert "12시간" in home.text
        first = client.post("/api/tick")
        assert first.status_code == 200
        body = first.json()
        assert body["tick"]["headline_cppi"] is not None
        assert body["tick"]["cppi_delta_1m"] is None
        state["call"] = 240
        state["put"] = 20
        second = client.post("/api/tick")
        assert second.status_code == 200
        tick = second.json()["tick"]
        assert tick["cppi_delta_1m"] is not None
        assert tick["flow_1m"] is not None
        assert tick["flow_1m"] > 0
        live = client.get("/api/live")
        assert live.status_code == 200
        assert live.json()["tick"]["symbol"] == "QQQ"
        minutes = client.get("/api/minutes")
        assert minutes.status_code == 200
        assert len(minutes.json()["points"]) >= 1
        five = client.get("/api/minutes?tf=5")
        assert five.status_code == 200
        assert five.json()["tf"] == 5
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["interval"] == 60
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.json()["source"] in {"yahoo", "tastytrade"}


def test_settings_switches_otm_points(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.setattr("optionsignal.collector.fetch_chain", lambda **kwargs: _chain())
    app = create_app(start_collector=False, interval=60)
    with TestClient(app) as client:
        home = client.get("/")
        assert 'id="otmPoints"' in home.text
        assert "등가격 ±100포" in home.text
        base = client.post("/api/tick").json()["tick"]
        assert base["otm_points"] is None
        assert base["headline_call_strikes"] == [102, 104]
        assert base["headline_put_strikes"] == [96, 98]
        res = client.post("/api/settings?otm_points=2")
        assert res.status_code == 200
        tick = res.json()["tick"]
        assert tick["otm_points"] == 2
        assert tick["headline_call_strikes"] == [102, 102]
        assert tick["headline_put_strikes"] == [98, 98]
        assert res.json()["status"]["otm_points"] == 2
        back = client.post("/api/settings?otm_points=0").json()
        assert back["tick"]["otm_points"] is None
        assert back["status"]["otm_points"] is None


def test_live_does_not_502_when_tick_failed(tmp_path, monkeypatch):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    app = create_app(start_collector=False, interval=60)
    with TestClient(app) as client:
        client.app.state.hub.error = "yahoo blocked"
        live = client.get("/api/live")
        assert live.status_code == 200
        body = live.json()
        assert body["tick"] is None
        assert body["status"]["error"] == "yahoo blocked"


def test_tick_nq_without_tasty_is_config_error(tmp_path, monkeypatch):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.delenv("TASTYTRADE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TASTYTRADE_REFRESH_TOKEN", raising=False)
    app = create_app(symbol="/NQ", start_collector=False, interval=5)
    with TestClient(app) as client:
        res = client.post("/api/tick")
        assert res.status_code == 400
        assert "Yahoo" in res.json()["detail"]
        live = client.get("/api/live")
        assert live.status_code == 200
        assert live.json()["status"]["error"]


def test_minutes_endpoint_strips_futures_slash(tmp_path, monkeypatch):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    from optionsignal.store import save_snapshot

    save_snapshot({"symbol": "NQ", "asof": "2026-09-08T10:01:00-04:00", "headline_cppi": 0.2, "futures_price": 24700})
    app = create_app(symbol="/NQ", start_collector=False, interval=5)
    with TestClient(app) as client:
        res = client.get("/api/minutes?tf=5")
        assert res.status_code == 200
        body = res.json()
        assert body["tf"] == 5
        assert len(body["points"]) == 1
        assert body["points"][0]["nq_close"] == 24700


def test_tick_tasty_connecting_is_503(tmp_path, monkeypatch):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")

    class Feed:
        ready = False
        error = None
        contracts = []
        _spot = None

        def snapshot(self):
            from optionsignal.tasty import FeedNotReady

            raise FeedNotReady("tastytrade DXLink 연결 중입니다.")

        def stop(self):
            return None

    app = create_app(symbol="/NQ", start_collector=False, interval=5)
    with TestClient(app) as client:
        client.app.state.hub.tasty_feed = Feed()
        res = client.post("/api/tick")
        assert res.status_code == 503
        assert "연결" in res.json()["detail"]


def test_chain_endpoint_returns_rows(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.setattr("optionsignal.collector.fetch_chain", lambda **kwargs: _chain())
    app = create_app(start_collector=False, interval=60)
    with TestClient(app) as client:
        home = client.get("/")
        assert 'id="tabChain"' in home.text
        assert 'id="chainRows"' in home.text
        empty = client.get("/api/chain")
        assert empty.status_code == 200
        assert empty.json()["rows"] == []
        client.post("/api/tick")
        res = client.get("/api/chain")
        assert res.status_code == 200
        body = res.json()
        assert body["spot"] == 100.0
        assert body["atm"] == 100
        strikes = [r["strike"] for r in body["rows"]]
        assert strikes == [96, 98, 100, 102, 104]
        atm_row = body["rows"][2]
        assert atm_row["call"]["volume"] == 80
        assert atm_row["put"]["open_interest"] == 40
        assert atm_row["call"]["delta"] is not None


def test_option_series_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    state = {"mid": 1.2}
    def chain(**kwargs):
        c = _chain()
        c.quotes = [q.__class__(**{**q.__dict__, "mid": state["mid"]}) if q.right == "call" and q.strike == 100 else q for q in c.quotes]
        return c
    monkeypatch.setattr("optionsignal.collector.fetch_chain", chain)
    app = create_app(start_collector=False, interval=60)
    with TestClient(app) as client:
        home = client.get("/")
        assert 'id="pickStrike"' in home.text
        assert 'id="pickChips"' in home.text
        assert client.get("/api/option_series?keys=100C").json()["series"]["100C"]["bars"] == []
        client.post("/api/tick")
        state["mid"] = 1.5
        client.post("/api/tick")
        res = client.get("/api/option_series?keys=100C,100P,nope")
        assert res.status_code == 200
        series = res.json()["series"]
        assert set(series) == {"100C", "100P", "NOPE"}
        call = series["100C"]
        assert call["latest"]["price"] == 1.5
        assert call["bars"][-1]["close"] == 1.5
        assert call["bars"][-1]["open"] == 1.2
        assert series["100P"]["latest"]["price"] == 1.0
        assert series["NOPE"]["bars"] == [] and series["NOPE"]["latest"] is None


def test_premium_ratio_endpoint(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    from optionsignal.store import save_option_ticks
    from tests.test_metrics import quote

    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    app = create_app(symbol="/NQ", start_collector=False)
    hub = app.state.hub
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=3)
    for i, (c, p) in enumerate([(10.0, 5.0), (12.0, 5.0), (9.0, 6.0)]):
        save_option_ticks("/NQ", [quote("call", 24700, c, volume=100), quote("put", 24700, p, volume=100)],
                          now=base + timedelta(minutes=i))
    hub.front_expiry = quote("call", 24700, 1.0).expiry
    with TestClient(app) as client:
        res = client.get("/api/premium_ratio?calls=24700C&puts=24700P&tf=1")
        assert res.status_code == 200
        data = res.json()
        assert data["calls"] == ["24700C"] and data["puts"] == ["24700P"]
        assert [round(b["close"]) for b in data["bars"]] == [200, 240, 150]
        assert round(data["latest"]["ratio"]) == 150
        empty = client.get("/api/premium_ratio?calls=24700C&puts=").json()
        assert empty["bars"] == [] and empty["latest"] is None


def test_settings_symbol_switch_without_tasty_keeps_yahoo_flow(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.setattr("optionsignal.collector.tasty_configured", lambda: False)
    app = create_app(symbol="QQQ", start_collector=False)
    hub = app.state.hub
    calls = []

    def fake_fetch(symbol, max_dte=0, expiry_limit=1, **kw):
        calls.append(symbol)
        raise RuntimeError("no yahoo in tests")

    hub.fetch_fn = fake_fetch
    with TestClient(app) as client:
        res = client.post("/api/settings?symbol=/ES")
        # /ES without tastytrade keys is a configuration error, not a crash.
        assert res.status_code == 400
        status = client.get("/api/status").json()
        assert status["symbol"] == "/ES"
        assert status["futures"] == {"code": "ES", "name_ko": "S&P 500 선물", "yahoo": "ES=F", "multiplier": 50}
