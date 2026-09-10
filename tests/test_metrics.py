from __future__ import annotations

from datetime import date, datetime, timezone
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


def test_select_near_otm_by_points_is_otm_only_and_mirrored():
    from optionsignal.metrics import otm_windows, select_near_otm, selection_window

    spot = 24712.0
    quotes = []
    for strike in range(24400, 25025, 25):
        quotes.append(quote("call", strike, 10.0, volume=5))
        quotes.append(quote("put", strike, 10.0, volume=5))
    calls, puts = select_near_otm(quotes, spot, band=0.08, points=100)
    # ATM strike 24700 and every ITM quote are dropped on both sides:
    # calls strictly above spot out to 24812, puts strictly below spot down to 24612.
    assert [q.strike for q in calls] == [24725, 24750, 24775, 24800]
    assert [q.strike for q in puts] == [24625, 24650, 24675]
    assert otm_windows(spot, 0.08, 100) == ((24712.0, 24812.0), (24612.0, 24712.0))
    assert selection_window(spot, 0.08, 100) == (24612.0, 24812.0)
    calls200, puts200 = select_near_otm(quotes, spot, band=0.08, points=200)
    assert min(q.strike for q in calls200) == 24725 and max(q.strike for q in calls200) == 24900
    assert min(q.strike for q in puts200) == 24525 and max(q.strike for q in puts200) == 24675
    # points=None or 0 falls back to the percent band, still OTM only.
    band_calls, band_puts = select_near_otm(quotes, spot, band=0.08, points=0)
    assert min(q.strike for q in band_calls) == 24725 and max(q.strike for q in band_calls) == 25000
    assert min(q.strike for q in band_puts) == 24400 and max(q.strike for q in band_puts) == 24675


def test_select_all_otm_excludes_atm_and_itm():
    from optionsignal.metrics import select_all_otm

    quotes = []
    for strike in (96, 98, 100, 102, 104):
        quotes.append(quote("call", strike, 1.0, volume=1))
        quotes.append(quote("put", strike, 1.0, volume=1))
    calls, puts = select_all_otm(quotes, 100.0)
    assert [q.strike for q in calls] == [102, 104]
    assert [q.strike for q in puts] == [96, 98]
    # spot between strikes: the nearest strike is ATM and still excluded.
    calls, puts = select_all_otm(quotes, 100.6)
    assert [q.strike for q in calls] == [102, 104]
    assert [q.strike for q in puts] == [96, 98]


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
    assert report.headline_call_strikes == [24725, 24850]
    assert report.headline_put_strikes == [24575, 24675]
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


def test_session_date_rolls_to_next_day_at_4pm_new_york():
    from datetime import timedelta

    from optionsignal.metrics import days_to_expiry, session_date

    before = datetime(2026, 9, 8, 15, 59, tzinfo=NY)
    at_close = datetime(2026, 9, 8, 16, 0, tzinfo=NY)
    evening_utc = datetime(2026, 9, 8, 22, 30, tzinfo=timezone.utc)  # 18:30 New York
    assert session_date(before) == date(2026, 9, 8)
    assert session_date(at_close) == date(2026, 9, 9)
    assert session_date(evening_utc) == date(2026, 9, 9)
    assert days_to_expiry(date(2026, 9, 8), before) == 0
    assert days_to_expiry(date(2026, 9, 8), at_close) == -1
    assert days_to_expiry(date(2026, 9, 9), at_close) == 0
    # Friday evening: Saturday has no expiry, Monday is the nearest living one (DTE 2).
    friday_night = datetime(2026, 9, 11, 19, 0, tzinfo=NY)
    assert session_date(friday_night) == date(2026, 9, 12)
    assert days_to_expiry(date(2026, 9, 14), friday_night) == 2
    assert session_date(before + timedelta(hours=24)) == date(2026, 9, 9)


def test_build_report_after_close_uses_next_expiry_as_0dte():
    from optionsignal.signal import chain_table, front_expiry_quotes

    today, tomorrow = date(2026, 9, 8), date(2026, 9, 9)
    quotes = []
    for strike in range(24600, 24825, 25):
        quotes.append(quote("call", strike, 10.0, volume=50, expiry=today))
        quotes.append(quote("put", strike, 10.0, volume=10, expiry=today))
        quotes.append(quote("call", strike, 30.0, volume=20, expiry=tomorrow))
        quotes.append(quote("put", strike, 30.0, volume=20, expiry=tomorrow))
    evening = datetime(2026, 9, 8, 16, 5, tzinfo=NY)
    chain = OptionChain(
        symbol="NQ", spot=24712.0, futures_symbol="/NQ", futures_price=24712.0,
        asof=evening, quotes=quotes, multiplier=20, source="test",
    )
    report = build_report(chain, max_dte=0)
    assert report.session_date == "2026-09-09"
    assert report.headline_expiry == "2026-09-09"
    assert report.headline_dte == 0
    assert report.headline_cppi == 0.0  # tomorrow's calls == puts; today's expired contracts ignored
    assert [s.expiry for s in report.slices] == ["2026-09-09"]
    expiry, front = front_expiry_quotes(chain, 0)
    assert expiry == tomorrow and all(q.expiry == tomorrow for q in front)
    table = chain_table(chain, 0)
    assert table["expiry"] == "2026-09-09" and table["dte"] == 0 and table["session_date"] == "2026-09-09"

    noon = datetime(2026, 9, 8, 12, 0, tzinfo=NY)
    chain_noon = OptionChain(
        symbol="NQ", spot=24712.0, futures_symbol="/NQ", futures_price=24712.0,
        asof=noon, quotes=quotes, multiplier=20, source="test",
    )
    day_report = build_report(chain_noon, max_dte=0)
    assert day_report.headline_expiry == "2026-09-08" and day_report.headline_cppi > 0.5


def test_build_report_computes_every_cppi_variant():
    quotes = []
    for strike in range(24000, 25425, 25):
        quotes.append(quote("call", strike, 10.0, volume=50 if strike <= 24800 else 5))
        quotes.append(quote("put", strike, 10.0, volume=5))
    chain = OptionChain(
        symbol="NQ", spot=24712.0, futures_symbol="/NQ", futures_price=24712.0,
        asof=NOW, quotes=quotes, multiplier=20, source="test",
    )
    report = build_report(chain)
    assert report.moneyness_band == 0.03
    v = report.cppi_variants
    assert set(v) == {"all", "p100", "p150", "p200", "p300", "chain"}
    # "chain" is the whole chain, ITM and ATM included: every quoted strike on both sides.
    assert v["chain"]["call_strikes"] == [24000, 25400] and v["chain"]["put_strikes"] == [24000, 25400]
    assert v["chain"]["call_count"] == 57 and v["chain"]["put_count"] == 57
    assert v["chain"]["call_window"] == [24000, 25400] and v["chain"]["put_window"] == [24000, 25400]
    assert v["chain"]["call_premium"] > v["all"]["call_premium"]
    assert v["p100"]["call_strikes"] == [24725, 24800] and v["p100"]["put_strikes"] == [24625, 24675]
    assert v["p300"]["call_strikes"] == [24725, 25000] and v["p300"]["put_strikes"] == [24425, 24675]
    assert v["all"]["band"] is None and v["all"]["points"] is None
    assert v["all"]["call_strikes"] == [24725, 25400] and v["all"]["put_strikes"] == [24000, 24675]
    assert v["p100"]["cppi"] > v["p300"]["cppi"]  # far calls have little volume, diluting p300
    assert v["all"]["bias"] in {"call", "mild_call", "neutral", "mild_put", "put"}
    assert v["all"]["call_count"] == 28 and v["all"]["put_count"] == 28


def test_cppi_variant_reports_rule_window_and_counts():
    quotes = []
    for strike in range(24000, 25425, 25):
        quotes.append(quote("call", strike, 10.0, volume=5))
        quotes.append(quote("put", strike, 10.0, volume=5))
    chain = OptionChain(
        symbol="NQ", spot=24700.0, futures_symbol="/NQ", futures_price=24700.0,
        asof=NOW, quotes=quotes, multiplier=20, source="test",
    )
    v = build_report(chain).cppi_variants
    every = v["all"]
    assert every["call_window"] == [24700, 25400] and every["put_window"] == [24000, 24700]
    assert every["call_count"] == 28 and every["put_count"] == 28
    p100 = v["p100"]
    assert p100["call_window"] == [24700, 24800] and p100["put_window"] == [24600, 24700]
    # spot sits on the 24700 strike: it is ATM and excluded, so 4 strikes a side.
    assert p100["call_count"] == 4 and p100["put_count"] == 4
