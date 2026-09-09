from __future__ import annotations

import json
from pathlib import Path

from optionsignal.models import SignalReport
from optionsignal.store import load_history, save_snapshot


def test_snapshot_roundtrip(tmp_path: Path):
    db = tmp_path / "sig.db"
    report = SignalReport(
        symbol="QQQ",
        spot=400.0,
        futures_symbol="NQ=F",
        futures_price=18000.0,
        asof="2026-09-08T12:00:00-04:00",
        source="test",
        max_dte=7,
        moneyness_band=0.08,
        headline_cppi=0.2,
        headline_call_premium=100,
        headline_put_premium=80,
        headline_call_notional=10000,
        headline_put_notional=8000,
        headline_call_volume=50,
        headline_put_volume=40,
        premium_ratio=1.25,
        risk_reversal=-0.04,
        otm_call_iv=0.18,
        otm_put_iv=0.22,
        session_call_surge_pct=5.0,
        session_put_surge_pct=1.0,
        session_surge_gap=4.0,
        bias="mild_call",
        score=1,
        summary_ko="테스트",
        summary_en="test",
        slices=[],
    )
    save_snapshot(report, path=db)
    rows = load_history("QQQ", path=db)
    assert len(rows) == 1
    assert rows[0]["headline_cppi"] == 0.2
    json.dumps(rows[0])


def test_aggregate_bars_1m_and_5m():
    from optionsignal.store import aggregate_bars

    ticks = [
        {"asof": "2026-09-08T10:01:05-04:00", "headline_cppi": 0.10, "headline_call_premium": 100, "headline_put_premium": 80},
        {"asof": "2026-09-08T10:01:20-04:00", "headline_cppi": 0.40, "headline_call_premium": 140, "headline_put_premium": 70},
        {"asof": "2026-09-08T10:04:10-04:00", "headline_cppi": -0.20, "headline_call_premium": 90, "headline_put_premium": 110},
        {"asof": "2026-09-08T10:06:00-04:00", "headline_cppi": 0.05, "headline_call_premium": 95, "headline_put_premium": 90},
    ]
    one = aggregate_bars(ticks, minutes=1)
    assert len(one) == 3
    assert one[0]["open"] == 0.10
    assert one[0]["high"] == 0.40
    assert one[0]["low"] == 0.10
    assert one[0]["close"] == 0.40
    assert one[0]["call_premium_delta_1m"] == 40
    five = aggregate_bars(ticks, minutes=5)
    assert len(five) == 2
    assert five[0]["open"] == 0.10
    assert five[0]["close"] == -0.20
    assert five[0]["high"] == 0.40
    assert five[0]["low"] == -0.20
    assert five[1]["cppi_delta_1m"] == 0.05 - (-0.20)


def test_aggregate_bars_carries_nq_ohlc():
    from optionsignal.store import aggregate_bars

    ticks = [
        {"asof": "2026-09-08T10:01:05-04:00", "headline_cppi": 0.1, "futures_price": 24700},
        {"asof": "2026-09-08T10:01:20-04:00", "headline_cppi": 0.2, "futures_price": 24760},
        {"asof": "2026-09-08T10:01:40-04:00", "headline_cppi": 0.15, "futures_price": 24680},
        {"asof": "2026-09-08T10:02:10-04:00", "headline_cppi": 0.0, "futures_price": 24720},
    ]
    one = aggregate_bars(ticks, minutes=1)
    assert one[0]["nq_open"] == 24700
    assert one[0]["nq_high"] == 24760
    assert one[0]["nq_low"] == 24680
    assert one[0]["nq_close"] == 24680
    five = aggregate_bars(one, minutes=5)
    assert five[0]["nq_open"] == 24700
    assert five[0]["nq_high"] == 24760
    assert five[0]["nq_low"] == 24680
    assert five[0]["nq_close"] == 24720


def test_option_ticks_roundtrip_and_bars(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from optionsignal.store import load_option_series, option_key, price_bars, save_option_ticks
    from tests.test_metrics import quote

    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    assert option_key(24700, "call") == "24700C"
    assert option_key(480.5, "put") == "480.5P"
    base = datetime(2026, 9, 8, 14, 0, 5, tzinfo=timezone.utc)
    prices = [40.0, 42.5, 39.0, 41.0, 45.0]
    for i, px in enumerate(prices):
        ts = base.replace(second=5 + 20 * (i % 3), minute=i // 3)
        n = save_option_ticks("/NQ", [quote("call", 24700, px, volume=100 + i), quote("put", 24700, 30.0)], now=ts)
        assert n == 2
    series = load_option_series("NQ", "24700C")
    assert [p["price"] for p in series] == prices
    assert series[-1]["volume"] == 104
    bars = price_bars(series, minutes=1)
    assert len(bars) == 2
    assert bars[0]["open"] == 40.0 and bars[0]["high"] == 42.5 and bars[0]["low"] == 39.0 and bars[0]["close"] == 39.0
    assert bars[1]["open"] == 41.0 and bars[1]["close"] == 45.0
    assert load_option_series("NQ", "99999C") == []


def test_aggregate_bars_merges_backfilled_nq_with_live_ticks():
    from optionsignal.store import aggregate_bars

    history = [
        {"asof": "2026-09-08T09:58:00+00:00", "nq_open": 24600, "nq_high": 24610, "nq_low": 24590, "nq_close": 24605, "backfill": True},
        {"asof": "2026-09-08T09:59:00+00:00", "nq_open": 24605, "nq_high": 24620, "nq_low": 24600, "nq_close": 24615, "backfill": True},
        {"asof": "2026-09-08T10:00:00+00:00", "nq_open": 24615, "nq_high": 24630, "nq_low": 24610, "nq_close": 24625, "backfill": True},
    ]
    live = [
        {"asof": "2026-09-08T10:00:20+00:00", "headline_cppi": 0.1, "futures_price": 24628},
        {"asof": "2026-09-08T10:00:50+00:00", "headline_cppi": 0.2, "futures_price": 24640},
        {"asof": "2026-09-08T10:01:05+00:00", "headline_cppi": 0.15, "futures_price": 24635},
    ]
    bars = aggregate_bars(history + live, minutes=1)
    assert [b["asof"][11:16] for b in bars] == ["09:58", "09:59", "10:00", "10:01"]
    assert bars[0]["headline_cppi"] is None and bars[0]["cppi_delta_1m"] is None
    assert bars[0]["nq_close"] == 24605
    merged = bars[2]
    assert merged["nq_open"] == 24615
    assert merged["nq_high"] == 24640
    assert merged["nq_close"] == 24640
    assert merged["headline_cppi"] == 0.2 and merged["open"] == 0.1
    assert abs(bars[3]["cppi_delta_1m"] - (0.15 - 0.2)) < 1e-9
    five = aggregate_bars(bars, minutes=5)
    assert [b["asof"][11:16] for b in five] == ["09:55", "10:00"]
    assert five[0]["nq_open"] == 24600 and five[0]["nq_close"] == 24615
    assert five[1]["nq_close"] == 24635 and five[1]["headline_cppi"] == 0.15


def test_save_and_load_bars_and_option_backfill(tmp_path, monkeypatch):
    from optionsignal.store import (
        backfill_option_bars, futures_history_points, load_bars, load_option_series, save_bars, save_option_ticks,
    )
    from tests.test_metrics import quote
    from datetime import datetime, timezone

    monkeypatch.setattr("optionsignal.store.DEFAULT_DB", tmp_path / "sig.db")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    def t(minutes_ago):
        return (now.replace(microsecond=0) - __import__("datetime").timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    candles = [{"asof": t(3), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
               {"asof": t(2), "open": 1.5, "high": 1.8, "low": 1.2, "close": 1.6, "volume": 5}]
    assert save_bars("/NQ", "NQ", candles) == 2
    assert save_bars("/NQ", "NQ", candles) == 2  # idempotent
    loaded = load_bars("NQ", "NQ")
    assert [b["close"] for b in loaded] == [1.5, 1.6]
    pts = futures_history_points("NQ")
    assert pts[0]["nq_close"] == 1.5 and pts[0]["backfill"] is True

    # A live tick exists at t(1); backfill must stop before it and be replaceable.
    save_option_ticks("/NQ", [quote("call", 24700, 9.0)], now=now - __import__("datetime").timedelta(minutes=1))
    n = backfill_option_bars("NQ", "24700C", candles + [{"asof": t(0), "close": 7.0}])
    assert n == 2
    series = load_option_series("NQ", "24700C")
    assert [p["price"] for p in series] == [1.5, 1.6, 9.0]
    assert series[0]["bid"] is None and series[2]["bid"] is not None
    n2 = backfill_option_bars("NQ", "24700C", candles[:1])
    assert n2 == 1
    assert [p["price"] for p in load_option_series("NQ", "24700C")] == [1.5, 9.0]


def test_aggregate_bars_buckets_by_instant_across_timezones():
    from optionsignal.store import aggregate_bars

    bars = aggregate_bars([
        {"asof": "2026-09-08T07:35:00+00:00", "nq_open": 1, "nq_high": 3, "nq_low": 1, "nq_close": 2},
        {"asof": "2026-09-08T03:35:20-04:00", "headline_cppi": 0.1, "futures_price": 4},
    ])
    assert len(bars) == 1
    assert bars[0]["nq_open"] == 1 and bars[0]["nq_high"] == 4 and bars[0]["nq_close"] == 4
