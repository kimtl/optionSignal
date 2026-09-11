from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from optionsignal.collector import LiveHub
from optionsignal.tasty import FeedConfigError, LiveContract, StreamQuote, TastyFeed, snapshot_from_cache
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
    assert hub.live_payload()["points"], "live points should find /NQ ticks stored as NQ"


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


def test_stream_events_fill_delta_and_day_volume():
    from types import SimpleNamespace

    from optionsignal.tasty import TastyFeed

    feed = TastyFeed(symbol="/NQ")
    feed.underlying_symbol = "/NQU26:XCME"
    feed.contracts = [LiveContract(date(2026, 9, 8), "put", 24700, "./NQU26P24700", 12)]
    feed._apply_quote(SimpleNamespace(event_symbol="./NQU26P24700", bid_price=24, ask_price=26, bid_size=3, ask_size=4))
    feed._apply_greeks(SimpleNamespace(event_symbol="./NQU26P24700", volatility=0.21, delta=-0.42, price=25.5))
    feed._apply_trade(SimpleNamespace(event_symbol="./NQU26P24700", price=25.25, day_volume=1832))
    feed._apply_trade(SimpleNamespace(event_symbol="/NQU26:XCME", price=24712.5, day_volume=500000))
    chain = feed.snapshot()
    put = chain.quotes[0]
    assert put.delta == -0.42
    assert put.volume == 1832
    assert put.last == 25.25
    assert put.open_interest == 12
    assert chain.spot == 24712.5


def test_chain_table_pairs_calls_and_puts_by_strike():
    from optionsignal.signal import chain_table

    expiry = date(2026, 9, 8)
    quotes = [
        quote("call", 24700, 60.0, volume=900, iv=0.20, expiry=expiry),
        quote("put", 24700, 58.0, volume=1200, iv=0.21, expiry=expiry),
        quote("call", 24725, 48.0, volume=300, iv=0.20, expiry=expiry),
        quote("call", 24750, 40.0, volume=100, iv=0.20, expiry=date(2026, 9, 9)),
    ]
    chain = OptionChain(symbol="NQ", spot=24712.0, futures_symbol="/NQ", futures_price=24712.0,
                        asof=NOW, quotes=quotes, multiplier=20, source="tastytrade")
    table = chain_table(chain, max_dte=0)
    assert table["expiry"] == "2026-09-08"
    assert table["dte"] == 0
    assert table["atm"] == 24700
    assert [r["strike"] for r in table["rows"]] == [24700, 24725]
    row = table["rows"][0]
    assert row["call"]["volume"] == 900
    assert row["put"]["volume"] == 1200
    assert row["put"]["open_interest"] == 1200
    # No feed delta on these quotes, so it is modelled from IV: call ~0.5+, put = call - 1.
    assert row["call"]["delta_source"] == "model"
    assert 0.4 < row["call"]["delta"] < 0.7
    assert -0.6 < row["put"]["delta"] < -0.3
    assert table["rows"][1]["put"] is None


def test_candle_to_bar_strips_candle_suffix_and_reads_ms_time():
    from types import SimpleNamespace

    from optionsignal.tasty import _candle_to_bar

    ev = SimpleNamespace(event_symbol="./NQU26C24700:XCME{=1m,tho=true}", time=1788600000000,
                         open=41.0, high=43.0, low=40.5, close=42.25, volume=17)
    symbol, ts_ms, bar = _candle_to_bar(ev)
    assert symbol == "./NQU26C24700:XCME"
    assert ts_ms == 1788600000000
    assert bar["asof"].startswith("2026-09-")
    assert bar["close"] == 42.25 and bar["high"] == 43.0 and bar["volume"] == 17
    assert _candle_to_bar(SimpleNamespace(event_symbol="/NQU26:XCME{=1m}", time=1788600000000, close=None)) is None


def test_hub_backfill_saves_future_and_option_candles(tmp_path, monkeypatch):
    import asyncio

    from optionsignal.store import FUTURES_KEY, load_bars, load_option_series

    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")

    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=10)
    t0, t1 = base.isoformat(timespec="seconds"), (base + timedelta(minutes=1)).isoformat(timespec="seconds")

    class Feed:
        contracts = [LiveContract(date(2026, 9, 8), "call", 24700, "./NQU26C24700:XCME", 10),
                     LiveContract(date(2026, 9, 8), "put", 24700, "./NQU26P24700:XCME", 10)]
        underlying_symbol = "/NQU26:XCME"
        _session = object()
        expiry = date(2026, 9, 8)
        ready = True
        streaming = False
        error = None

        async def fetch_candles(self, hours=12):
            return {
                "/NQU26:XCME": [
                    {"asof": t0, "open": 24600, "high": 24610, "low": 24590, "close": 24605, "volume": 100},
                    {"asof": t1, "open": 24605, "high": 24620, "low": 24600, "close": 24615, "volume": 90},
                ],
                "./NQU26C24700:XCME": [{"asof": t1, "close": 55.5, "volume": 3}],
            }

    hub = LiveHub(symbol="/NQ", interval=5, tasty_feed=Feed())
    hub._running = True
    summary = asyncio.run(hub.backfill_history(hours=12))
    assert summary == {"source": "tastytrade", "nq_bars": 2, "options": 1, "expiry": "2026-09-08"}
    assert [b["close"] for b in load_bars("NQ", FUTURES_KEY)] == [24605, 24615]
    assert load_option_series("NQ", "24700C")[0]["price"] == 55.5
    assert load_option_series("NQ", "24700C", expiry="2026-09-08")[0]["price"] == 55.5
    assert load_option_series("NQ", "24700C", expiry="2026-09-09") == []
    assert load_option_series("NQ", "24700P") == []
    assert hub.status()["backfill"]["nq_bars"] == 2
    points = hub.live_payload()["points"]
    assert len(points) == 2
    assert points[0]["nq_close"] == 24605 and points[0]["headline_cppi"] is None


def test_hub_backfill_uses_yahoo_without_tasty(tmp_path, monkeypatch):
    import asyncio

    from optionsignal.store import FUTURES_KEY, load_bars

    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.setattr("optionsignal.collector.tasty_configured", lambda: False)
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    monkeypatch.setattr(
        "optionsignal.collector.fetch_price_history",
        lambda symbol, hours: [{"asof": recent, "open": 480, "high": 481, "low": 479.5, "close": 480.5, "volume": 1000}],
    )
    hub = LiveHub(symbol="QQQ", interval=60)
    hub._running = True
    summary = asyncio.run(hub.backfill_history(hours=12))
    assert summary["source"] == "yahoo" and summary["nq_bars"] == 1
    assert load_bars("QQQ", FUTURES_KEY)[0]["close"] == 480.5


def test_load_instruments_rolls_to_next_expiry_after_close(monkeypatch):
    import asyncio
    import sys
    import types
    from types import SimpleNamespace

    today, tomorrow = date(2026, 9, 8), date(2026, 9, 9)

    def option(expiry, strike, right):
        return SimpleNamespace(
            expiration_date=expiry, strike_price=strike, option_type=right,
            streamer_symbol=f"./NQU26{right}{strike}:XCME@{expiry:%d}", symbol=f"NQU26 {expiry:%y%m%d}{right}{strike}",
            days_to_expiration=(expiry - today).days, open_interest=5,
        )

    chain = {
        today: [option(today, s, r) for s in (24650, 24700, 24750) for r in ("C", "P")],
        tomorrow: [option(tomorrow, s, r) for s in (24650, 24700, 24750) for r in ("C", "P")],
    }

    async def fake_future_chain(session, root):
        return chain

    fake_instruments = types.ModuleType("tastytrade.instruments")
    fake_instruments.Future = object
    fake_instruments.get_future_option_chain = fake_future_chain
    fake_instruments.get_option_chain = fake_future_chain
    monkeypatch.setitem(sys.modules, "tastytrade.instruments", fake_instruments)

    async def no_future(*args, **kwargs):
        return None

    monkeypatch.setattr("optionsignal.tasty._front_future", no_future)

    feed = TastyFeed(symbol="/NQ", max_dte=0)
    feed._spot = 24700.0

    monkeypatch.setattr("optionsignal.tasty.session_date", lambda now=None: today)
    asyncio.run(feed._load_instruments(session=None))
    assert feed.session == today and feed.expiry == today
    assert {c.expiry for c in feed.contracts} == {today}
    assert not feed.rolled_over()

    # 4pm New York passed: the same chain now yields tomorrow's contracts as 0DTE.
    monkeypatch.setattr("optionsignal.tasty.session_date", lambda now=None: tomorrow)
    assert feed.rolled_over()
    assert feed.phase == "rollover"
    feed.quotes["./NQU26C24700:XCME@08"] = StreamQuote(bid=1, ask=2)
    asyncio.run(feed._load_instruments(session=None))
    assert feed.session == tomorrow and feed.expiry == tomorrow
    assert {c.expiry for c in feed.contracts} == {tomorrow}
    assert "./NQU26C24700:XCME@08" not in feed.quotes  # stale expired-contract quotes dropped


def test_ensure_feed_swaps_tasty_feed_when_symbol_changes(monkeypatch, tmp_path):
    import asyncio

    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    monkeypatch.setattr("optionsignal.collector.tasty_configured", lambda: True)
    created = []

    class FakeFeed:
        def __init__(self, symbol, max_dte=0, band=0.08):
            self.symbol = symbol
            self.contracts = []
            self.underlying_symbol = None
            self.stopped = False
            created.append(self)

        async def run(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

        def stop(self):
            self.stopped = True

    monkeypatch.setattr("optionsignal.collector.TastyFeed", FakeFeed)

    async def scenario():
        hub = LiveHub(symbol="/NQ", interval=5)
        hub._running = True
        assert await hub.ensure_feed() is True
        assert created[-1].symbol == "/NQ"
        assert await hub.ensure_feed() is False  # same product: nothing to do
        hub.symbol = "/ES"
        hub.latest = {"x": 1}
        assert await hub.ensure_feed() is True
        assert created[-1].symbol == "/ES" and created[0].stopped
        assert hub.latest is None and hub.tasty_feed is created[-1]
        assert hub.futures_info()["code"] == "ES"
        await hub._stop_task(hub._tasty_task)
        await hub._stop_task(hub._backfill_task)

    asyncio.run(scenario())
    assert len(created) == 2


def test_describe_exception_unwraps_task_group():
    from optionsignal.tasty import describe_exception

    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ConnectionResetError("websocket closed"), ExceptionGroup("inner", [TimeoutError()])],
    )
    text = describe_exception(group)
    assert "TaskGroup" not in text
    assert text == "ConnectionResetError: websocket closed / TimeoutError"
    assert describe_exception(ValueError("bad")) == "ValueError: bad"


def test_stream_failure_polls_rest_then_reconnects_without_board_error(monkeypatch):
    import asyncio

    import optionsignal.tasty as tasty

    monkeypatch.setattr(tasty, "STREAM_RETRY_MIN", 0.01)
    monkeypatch.setattr(tasty, "STREAM_RETRY_MAX", 0.02)
    feed = TastyFeed(symbol="/NQ", max_dte=0, band=0.08)
    feed.contracts = [LiveContract(date(2030, 1, 1), "call", 20000.0, "./NQ1C20000", 0, "occ")]
    feed._spot = 20000.0
    feed.ready = True
    feed._running = True
    calls = {"stream": 0, "rest": 0}

    async def failing_stream(*_args):
        calls["stream"] += 1
        if calls["stream"] == 1:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionResetError("ws dropped")])
        # second attempt "connects" and then the feed is stopped
        feed.streaming = True
        feed.stream_error = None
        feed._running = False

    async def rest(_session):
        calls["rest"] += 1
        return True

    monkeypatch.setattr(feed, "_stream", failing_stream)
    monkeypatch.setattr(feed, "_hydrate_quotes_rest", rest)

    asyncio.run(feed._stream_with_rest_fallback(object(), None, None, None, None))

    assert calls["stream"] == 2
    assert calls["rest"] >= 1
    assert feed.error is None  # REST kept the board alive: no red line
    assert feed.stream_retries == 1
    assert feed.streaming is True and feed.stream_error is None


def test_stream_failure_with_dead_rest_forces_relogin(monkeypatch):
    import asyncio

    import optionsignal.tasty as tasty

    monkeypatch.setattr(tasty, "STREAM_RETRY_MIN", 60.0)
    monkeypatch.setattr(tasty, "REST_FAILURES_BEFORE_RELOGIN", 2)
    feed = TastyFeed(symbol="/NQ", max_dte=0, band=0.08)
    feed.contracts = [LiveContract(date(2030, 1, 1), "call", 20000.0, "./NQ1C20000", 0, "occ")]
    feed._spot = 20000.0
    feed.ready = True
    feed._running = True

    async def failing_stream(*_args):
        raise RuntimeError("no dxlink")

    async def dead_rest(_session):
        return False

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(feed, "_stream", failing_stream)
    monkeypatch.setattr(feed, "_hydrate_quotes_rest", dead_rest)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)  # tasty imports asyncio lazily: same module object

    with pytest.raises(RuntimeError, match="재로그인"):
        asyncio.run(feed._stream_with_rest_fallback(object(), None, None, None, None))
    assert feed.stream_error == "DXLink: RuntimeError: no dxlink"


def test_status_shows_rest_fallback_when_stream_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")

    class Feed:
        ready = True
        streaming = False
        error = None
        stream_error = "DXLink: ConnectionResetError: ws dropped"
        stream_retries = 2
        phase = "rest"
        contracts = [object()]
        quoted_count = 1

        def stop(self):
            return None

    hub = LiveHub(symbol="/ES", interval=5, tasty_feed=Feed())
    status = hub.status()
    assert status["error"] is None
    assert status["stream_error"] == Feed.stream_error
    assert status["stream_retries"] == 2
    assert "DXLink 재연결" in status["session"]["label_ko"]
