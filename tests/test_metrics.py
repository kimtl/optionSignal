from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from optionsignal.metrics import (
    bias_from_metrics,
    call_delta,
    call_put_premium_imbalance,
    interpolate,
    option_mid,
    premium_ratio,
    risk_reversal_iv,
    sane_iv,
    volume_premium,
)
from optionsignal.models import OptionChain, OptionQuote
from optionsignal.signal import build_report

NY = ZoneInfo("America/New_York")
EXPIRY = date(2026, 9, 8)
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=NY)


def quote(
    right: str,
    strike: float,
    mid: float,
    volume: int = 10,
    iv: float = 0.20,
    pct: float = 0.0,
    expiry: date = EXPIRY,
) -> OptionQuote:
    return OptionQuote(
        expiry=expiry,
        right=right,  # type: ignore[arg-type]
        strike=strike,
        bid=max(mid - 0.05, 0.01),
        ask=mid + 0.05,
        last=mid,
        mid=mid,
        volume=volume,
        open_interest=volume,
        iv=iv,
        percent_change=pct,
    )


def test_option_mid_prefers_bid_ask():
    assert option_mid(1.0, 3.0, 9.0) == 2.0
    assert option_mid(None, None, 1.5) == 1.5
    assert option_mid(0, 0, None) is None


def test_sane_iv_rejects_garbage_0dte_wings():
    assert sane_iv(0.22) == 0.22
    assert sane_iv(3.5) is None
    assert sane_iv(0.0) is None


def test_cppi_balanced_and_one_sided():
    assert call_put_premium_imbalance(20, 20) == 0
    assert call_put_premium_imbalance(80, 20) == 0.6
    assert call_put_premium_imbalance(0, 50) == -1
    assert call_put_premium_imbalance(0, 0) is None
    assert premium_ratio(40, 20) == 2


def test_interpolate_brackets_target():
    assert interpolate([0.1, 0.3], [0.2, 0.4], 0.2) == pytest.approx(0.3)
    assert interpolate([0.1, 0.3], [0.2, 0.4], 0.0) == 0.2


def test_call_delta_atm_near_half():
    delta = call_delta(100, 100, 7 / 365, 0.2)
    assert delta is not None
    assert 0.45 < delta < 0.55


def test_volume_premium_skips_bad_mids():
    quotes = [
        quote("call", 100, 2.0, volume=10),
        quote("call", 101, 0.0, volume=999),
    ]
    quotes[1] = OptionQuote(**{**quotes[1].__dict__, "mid": None})
    assert volume_premium(quotes) == 20.0


def test_risk_reversal_negative_when_puts_rich():
    spot = 100.0
    quotes = []
    for strike in range(90, 111, 2):
        quotes.append(quote("call", strike, 1.5, iv=0.18, volume=50))
        quotes.append(quote("put", strike, 1.5, iv=0.28, volume=50))
    rr = risk_reversal_iv(quotes, spot, EXPIRY, now=NOW)
    assert rr is not None
    assert rr < -0.05


def test_build_report_call_heavy():
    quotes = []
    for strike in (96, 98, 100, 102, 104, 106):
        call_vol = 200 if strike >= 100 else 20
        put_vol = 20 if strike <= 100 else 5
        quotes.append(quote("call", strike, 1.2, volume=call_vol, iv=0.25, pct=12))
        quotes.append(quote("put", strike, 1.0, volume=put_vol, iv=0.22, pct=1))
    chain = OptionChain(
        symbol="QQQ",
        spot=100.0,
        futures_symbol="NQ=F",
        futures_price=20000.0,
        asof=NOW,
        quotes=quotes,
        multiplier=100,
        source="test",
    )
    report = build_report(chain, max_dte=7, moneyness_band=0.08)
    assert report.headline_cppi is not None
    assert report.headline_cppi > 0.3
    assert report.bias in {"call", "mild_call"}
    assert report.score > 0
    assert report.slices
    assert report.headline_call_notional == report.headline_call_premium * 100


def test_build_report_put_heavy():
    quotes = []
    for strike in (96, 98, 100, 102, 104):
        quotes.append(quote("call", strike, 0.8, volume=10, iv=0.18, pct=-2))
        quotes.append(quote("put", strike, 1.4, volume=180, iv=0.30, pct=20))
    chain = OptionChain(
        symbol="QQQ",
        spot=100.0,
        futures_symbol="NQ=F",
        futures_price=20000.0,
        asof=NOW,
        quotes=quotes,
        source="test",
    )
    report = build_report(chain)
    assert report.headline_cppi is not None and report.headline_cppi < -0.3
    assert report.bias in {"put", "mild_put"}
    assert report.session_surge_gap is not None and report.session_surge_gap < 0


def test_select_near_otm_by_points_uses_atm_strike_out_to_n_points():
    from optionsignal.metrics import select_near_otm

    spot = 24712.0
    quotes = []
    for strike in range(24400, 25025, 25):
        quotes.append(quote("call", strike, 10.0, volume=5))
        quotes.append(quote("put", strike, 10.0, volume=5))
    calls, puts = select_near_otm(quotes, spot, band=0.08, points=100)
    # ATM strike is 24700; calls run 24700..24800, puts 24625..24700.
    assert [q.strike for q in calls] == [24700, 24725, 24750, 24775, 24800]
    assert [q.strike for q in puts] == [24625, 24650, 24675, 24700]
    calls200, puts200 = select_near_otm(quotes, spot, band=0.08, points=200)
    assert max(q.strike for q in calls200) == 24900
    assert min(q.strike for q in puts200) == 24525
    # points=None or 0 falls back to the percent band.
    band_calls, band_puts = select_near_otm(quotes, spot, band=0.08, points=0)
    assert band_calls and band_puts
    assert max(q.strike for q in band_calls) == 25000


def test_build_report_points_mode_reports_strike_range():
    quotes = []
    for strike in range(24400, 25025, 25):
        quotes.append(quote("call", strike, 10.0, volume=50 if strike >= 24700 else 5))
        quotes.append(quote("put", strike, 10.0, volume=5))
    chain = OptionChain(
        symbol="NQ", spot=24712.0, futures_symbol="/NQ", futures_price=24712.0,
        asof=NOW, quotes=quotes, multiplier=20, source="test",
    )
    report = build_report(chain, otm_points=150)
    assert report.otm_points == 150
    assert report.headline_call_strikes == [24700, 24850]
    assert report.headline_put_strikes == [24575, 24700]
    assert report.headline_cppi is not None and report.headline_cppi > 0.5
    payload = report.to_dict()
    assert payload["otm_points"] == 150
    band_report = build_report(chain, otm_points=0)
    assert band_report.otm_points is None
    assert band_report.headline_call_strikes[1] == 25000


def test_bias_thresholds():
    bias, score, ko, en = bias_from_metrics(0.4, 0.04, 10)
    assert bias == "call" and score == 2
    assert "콜" in ko
    bias, score, _, _ = bias_from_metrics(0.0, 0.0, 0.0)
    assert bias == "neutral" and score == 0
