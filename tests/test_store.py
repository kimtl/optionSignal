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
