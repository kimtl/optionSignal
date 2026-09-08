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
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["interval"] == 60
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
