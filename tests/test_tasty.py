from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from optionsignal.collector import LiveHub
from optionsignal.tasty import FeedConfigError, LiveContract, StreamQuote, snapshot_from_cache
from tests.test_metrics import NOW, quote
from optionsignal.models import OptionChain

NY = ZoneInfo("America/New_York")


def test_right_reads_tasty_put_abbrev():
    from enum import StrEnum

    from optionsignal.tasty import _right

    class OptionType(StrEnum):
        CALL = "C"
        PUT = "P"

    assert _right(OptionType.PUT) == "put"
    assert _right(OptionType.CALL) == "call"
    assert _right("P") == "put"
    assert _right("C") == "call"
    assert _right("Put") == "put"
    assert _right("", "./NQU26P24700") == "put"
    assert _right("", "./NQU26C24700") == "call"


def test_right_reads_installed_tastytrade_enum():
    from tastytrade.instruments import OptionType

    from optionsignal.tasty import _right

    assert OptionType.PUT == "P"
    assert _right(OptionType.PUT) == "put"
    assert _right(OptionType.CALL) == "call"
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
        streaming = True
        error = None
        phase = "live"
        contracts = []
        quoted_count = 10

        def stop(self):
            return None

    hub = LiveHub(symbol="/NQ", interval=5, tasty_feed=Feed())
    payload = hub.collect_once()
    assert payload["source"] == "tastytrade"
    assert payload["headline_cppi"] is not None
    assert hub.status()["source"] == "tastytrade"
    assert hub.status()["realtime"] is True
    assert hub.status()["session"]["code"] == "tasty"


def test_pick_front_future_skips_product_root_style_dates():
    from types import SimpleNamespace
    from optionsignal.tasty import pick_front_future

    today = date(2026, 9, 8)
    items = [
        SimpleNamespace(symbol="/NQZ5", active_month=False, expiration_date=date(2025, 12, 19)),
        SimpleNamespace(symbol="/NQU6", active_month=True, expiration_date=date(2026, 9, 18)),
        SimpleNamespace(symbol="/NQZ6", active_month=False, expiration_date=date(2026, 12, 18)),
    ]
    picked = pick_front_future(items, today=today)
    assert picked.symbol == "/NQU6"


def test_nearest_contracts_keeps_strikes_closest_to_spot():
    from optionsignal.tasty import LiveContract, nearest_contracts

    expiry = date(2026, 9, 8)
    contracts = []
    for strike in range(20000, 30000, 100):
        contracts.append(LiveContract(expiry, "call", float(strike), f".C{strike}"))
        contracts.append(LiveContract(expiry, "put", float(strike), f".P{strike}"))
    kept = nearest_contracts(contracts, 24700, limit=8)
    strikes = {c.strike for c in kept}
    assert len(kept) == 8
    assert 24700 in strikes
    assert max(strikes) - min(strikes) <= 400


def test_status_not_stuck_connecting_when_rest_ready(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")

    class Feed:
        ready = True
        streaming = False
        error = None
        phase = "quotes"
        contracts = [object(), object()]
        quoted_count = 2

        def snapshot(self):
            raise AssertionError("unused")

        def stop(self):
            return None

    hub = LiveHub(symbol="/NQ", interval=5, tasty_feed=Feed())
    status = hub.status()
    assert status["session"]["code"] == "tasty-rest"
    assert status["realtime"] is False
    assert status["ready"] is True
    assert status["contracts"] == 2


def test_hub_nq_without_tasty_does_not_call_yahoo(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.delenv("TASTYTRADE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TASTYTRADE_REFRESH_TOKEN", raising=False)

    def boom(**kwargs):
        raise AssertionError("Yahoo must not be used for /NQ")

    hub = LiveHub(symbol="/NQ", interval=5, fetch_fn=boom)
    with pytest.raises(FeedConfigError, match="Yahoo"):
        hub.collect_once()


def test_snapshot_raises_not_ready_before_chain():
    from optionsignal.tasty import FeedNotReady, TastyFeed

    feed = TastyFeed(symbol="/NQ")
    with pytest.raises(FeedNotReady, match="체인"):
        feed.snapshot()
