from datetime import date, datetime
from zoneinfo import ZoneInfo

from optionsignal.collector import LiveHub
from optionsignal.tasty import LiveContract, StreamQuote, snapshot_from_cache
from tests.test_metrics import NOW, quote
from optionsignal.models import OptionChain

NY = ZoneInfo("America/New_York")


def test_snapshot_uses_bid_ask_mid_and_size_as_volume():
    expiry = date(2026, 9, 8)
    contracts = [
        LiveContract(expiry, "call", 24700, ".NQ1C", 10),
        LiveContract(expiry, "put", 24700, ".NQ1P", 12),
    ]
    quotes = {
        ".NQ1C": StreamQuote(bid=20, ask=22, bid_size=8, ask_size=12, iv=0.18),
        ".NQ1P": StreamQuote(bid=24, ask=26, bid_size=30, ask_size=10, iv=0.22),
    }
    chain = snapshot_from_cache(
        contracts,
        quotes,
        symbol="/NQ",
        spot=24700,
        futures_price=24700,
        now=datetime(2026, 9, 8, 10, 0, tzinfo=NY),
    )
    assert chain.source == "tastytrade"
    assert chain.symbol == "NQ"
    assert chain.quotes[0].mid == 21
    assert chain.quotes[0].volume == 20
    assert chain.quotes[1].mid == 25
    assert chain.quotes[1].iv == 0.22


def test_hub_reads_tasty_feed(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")

    class Feed:
        def snapshot(self):
            quotes = []
            for strike in (96, 98, 100, 102, 104):
                quotes.append(quote("call", strike, 1.2, volume=80, iv=0.22, pct=8))
                quotes.append(quote("put", strike, 1.0, volume=40, iv=0.24, pct=2))
            return OptionChain(
                symbol="NQ",
                spot=100.0,
                futures_symbol="NQ=F",
                futures_price=24700.0,
                asof=NOW,
                quotes=quotes,
                source="tastytrade",
            )

        ready = True
        error = None

        def stop(self):
            return None

    hub = LiveHub(symbol="/NQ", interval=5, tasty_feed=Feed())
    payload = hub.collect_once()
    assert payload["source"] == "tastytrade"
    assert payload["headline_cppi"] is not None
    assert hub.status()["source"] == "tastytrade"
    assert hub.status()["realtime"] is True
