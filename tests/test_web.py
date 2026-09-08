from __future__ import annotations

from fastapi.testclient import TestClient

from optionsignal.models import OptionChain
from optionsignal.web import create_app
from tests.test_metrics import NOW, quote


def _chain() -> OptionChain:
    quotes = []
    for strike in (96, 98, 100, 102, 104):
        quotes.append(quote("call", strike, 1.2, volume=80, iv=0.22, pct=8))
        quotes.append(quote("put", strike, 1.0, volume=40, iv=0.24, pct=2))
    return OptionChain(
        symbol="QQQ",
        spot=100.0,
        futures_symbol="NQ=F",
        futures_price=20000.0,
        asof=NOW,
        quotes=quotes,
        source="test",
    )


def test_dashboard_and_signal(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.web.fetch_chain", lambda **kwargs: _chain())
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.setattr("optionsignal.web.load_history", lambda symbol, limit=200: [])
    app = create_app()
    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200
    assert "CPPI" in home.text
    signal = client.get("/api/signal?refresh=true")
    assert signal.status_code == 200
    body = signal.json()
    assert body["symbol"] == "QQQ"
    assert body["headline_cppi"] is not None
    hist = client.get("/api/history")
    assert hist.status_code == 200
    assert hist.json() == []
