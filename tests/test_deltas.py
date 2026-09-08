from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from optionsignal.signal import with_deltas, yahoo_session_status

NY = ZoneInfo("America/New_York")


def test_with_deltas_first_tick_has_no_gap():
    current = {
        "asof": "2026-09-08T10:01:00-04:00",
        "headline_cppi": 0.10,
        "headline_call_premium": 100,
        "headline_put_premium": 80,
    }
    out = with_deltas(current, [])
    assert out["cppi_delta_1m"] is None
    assert out["flow_1m"] is None


def test_with_deltas_measures_one_and_five_minutes():
    prior = []
    for minute, cppi, call_p, put_p in [
        (0, -0.10, 100, 140),
        (1, -0.08, 110, 130),
        (2, 0.00, 150, 120),
        (3, 0.05, 180, 110),
        (4, 0.08, 200, 100),
    ]:
        prior.append({
            "asof": (datetime(2026, 9, 8, 10, 0, tzinfo=NY) + timedelta(minutes=minute)).isoformat(),
            "headline_cppi": cppi,
            "headline_call_premium": call_p,
            "headline_put_premium": put_p,
        })
    current = {
        "asof": "2026-09-08T10:05:00-04:00",
        "headline_cppi": 0.20,
        "headline_call_premium": 260,
        "headline_put_premium": 90,
    }
    out = with_deltas(current, prior)
    assert out["cppi_delta_1m"] == pytest.approx(0.12)
    assert out["call_premium_delta_1m"] == 60
    assert out["put_premium_delta_1m"] == -10
    assert out["flow_1m"] > 0.5
    assert abs(out["cppi_delta_5m"] - 0.30) < 1e-9


def test_session_weekend_and_rth():
    weekend = yahoo_session_status(datetime(2026, 9, 6, 12, 0, tzinfo=NY))
    assert weekend["live"] is False
    rth = yahoo_session_status(datetime(2026, 9, 8, 10, 0, tzinfo=NY))
    assert rth["code"] == "rth" and rth["live"] is True
    after = yahoo_session_status(datetime(2026, 9, 8, 20, 0, tzinfo=NY))
    assert after["live"] is False
