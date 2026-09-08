from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .metrics import (
    bias_from_metrics,
    call_put_premium_imbalance,
    is_near_otm,
    nearest_quote,
    premium_ratio,
    risk_reversal_iv,
    total_volume,
    volume_premium,
    volume_weighted_iv,
    volume_weighted_pct_change,
)
from .models import ExpirySlice, OptionChain, OptionQuote, SignalReport

NY = ZoneInfo("America/New_York")
DEFAULT_BAND = 0.08
DEFAULT_WING_PCT = 0.03
DEFAULT_HEADLINE_DTE = 7


def _dte(expiry, now: datetime) -> int:
    return (expiry - now.astimezone(NY).date()).days


def _group_by_expiry(quotes: list[OptionQuote]) -> dict:
    grouped: dict = defaultdict(list)
    for quote in quotes:
        grouped[quote.expiry].append(quote)
    return dict(sorted(grouped.items()))


def _slice_for_expiry(
    expiry,
    quotes: list[OptionQuote],
    spot: float,
    now: datetime,
    band: float,
    wing_pct: float,
) -> ExpirySlice:
    calls = [q for q in quotes if q.right == "call" and is_near_otm(q, spot, band)]
    puts = [q for q in quotes if q.right == "put" and is_near_otm(q, spot, band)]
    call_prem = volume_premium(calls)
    put_prem = volume_premium(puts)
    call_iv = volume_weighted_iv(calls)
    put_iv = volume_weighted_iv(puts)
    rr = risk_reversal_iv(quotes, spot, expiry, now=now)
    wing_call = nearest_quote([q for q in quotes if q.right == "call"], spot * (1 + wing_pct))
    wing_put = nearest_quote([q for q in quotes if q.right == "put"], spot * (1 - wing_pct))
    return ExpirySlice(
        expiry=expiry.isoformat(),
        dte=_dte(expiry, now),
        call_premium=round(call_prem, 2),
        put_premium=round(put_prem, 2),
        call_volume=total_volume(calls),
        put_volume=total_volume(puts),
        cppi=call_put_premium_imbalance(call_prem, put_prem),
        otm_call_iv=call_iv,
        otm_put_iv=put_iv,
        risk_reversal=rr,
        call_surge_pct=volume_weighted_pct_change(calls),
        put_surge_pct=volume_weighted_pct_change(puts),
        wing_call_mid=wing_call.mid if wing_call else None,
        wing_put_mid=wing_put.mid if wing_put else None,
        wing_pct=wing_pct,
    )


def build_report(
    chain: OptionChain,
    max_dte: int = DEFAULT_HEADLINE_DTE,
    moneyness_band: float = DEFAULT_BAND,
    wing_pct: float = DEFAULT_WING_PCT,
) -> SignalReport:
    now = chain.asof if chain.asof.tzinfo else chain.asof.replace(tzinfo=NY)
    slices: list[ExpirySlice] = []
    headline_calls: list[OptionQuote] = []
    headline_puts: list[OptionQuote] = []
    headline_all: list[OptionQuote] = []

    for expiry, quotes in _group_by_expiry(chain.quotes).items():
        dte = _dte(expiry, now)
        if dte < 0:
            continue
        slices.append(
            _slice_for_expiry(expiry, quotes, chain.spot, now, moneyness_band, wing_pct)
        )
        if dte <= max_dte:
            headline_all.extend(quotes)
            headline_calls.extend(
                q for q in quotes if q.right == "call" and is_near_otm(q, chain.spot, moneyness_band)
            )
            headline_puts.extend(
                q for q in quotes if q.right == "put" and is_near_otm(q, chain.spot, moneyness_band)
            )

    call_prem = volume_premium(headline_calls)
    put_prem = volume_premium(headline_puts)
    cppi = call_put_premium_imbalance(call_prem, put_prem)
    call_surge = volume_weighted_pct_change(headline_calls)
    put_surge = volume_weighted_pct_change(headline_puts)
    surge_gap = None
    if call_surge is not None and put_surge is not None:
        surge_gap = call_surge - put_surge

    nearest_expiry = None
    if headline_all:
        nearest_expiry = min(q.expiry for q in headline_all)
    rr = None
    if nearest_expiry is not None:
        rr = risk_reversal_iv(
            [q for q in headline_all if q.expiry == nearest_expiry],
            chain.spot,
            nearest_expiry,
            now=now,
        )

    otm_call_iv = volume_weighted_iv(headline_calls)
    otm_put_iv = volume_weighted_iv(headline_puts)
    if rr is None and otm_call_iv is not None and otm_put_iv is not None:
        rr = otm_call_iv - otm_put_iv

    bias, score, summary_ko, summary_en = bias_from_metrics(cppi, rr, surge_gap)
    ratio = premium_ratio(call_prem, put_prem)
    if ratio == float("inf"):
        ratio = None

    return SignalReport(
        symbol=chain.symbol,
        spot=chain.spot,
        futures_symbol=chain.futures_symbol,
        futures_price=chain.futures_price,
        asof=now.astimezone(NY).isoformat(timespec="seconds"),
        source=chain.source,
        max_dte=max_dte,
        moneyness_band=moneyness_band,
        headline_cppi=cppi,
        headline_call_premium=round(call_prem, 2),
        headline_put_premium=round(put_prem, 2),
        headline_call_notional=round(call_prem * chain.multiplier, 2),
        headline_put_notional=round(put_prem * chain.multiplier, 2),
        headline_call_volume=total_volume(headline_calls),
        headline_put_volume=total_volume(headline_puts),
        premium_ratio=ratio,
        risk_reversal=rr,
        otm_call_iv=otm_call_iv,
        otm_put_iv=otm_put_iv,
        session_call_surge_pct=call_surge,
        session_put_surge_pct=put_surge,
        session_surge_gap=surge_gap,
        bias=bias,
        score=score,
        summary_ko=summary_ko,
        summary_en=summary_en,
        slices=slices,
    )
