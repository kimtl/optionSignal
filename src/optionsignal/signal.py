from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .metrics import (
    atm_strike,
    bias_from_metrics,
    call_delta,
    call_put_premium_imbalance,
    days_to_expiry,
    nearest_quote,
    premium_ratio,
    risk_reversal_iv,
    sane_iv,
    select_near_otm,
    selection_window,
    session_date,
    strike_range,
    total_volume,
    volume_premium,
    volume_weighted_iv,
    volume_weighted_pct_change,
    year_fraction,
)
from .models import ExpirySlice, OptionChain, OptionQuote, SignalReport

NY = ZoneInfo("America/New_York")
DEFAULT_BAND = 0.03
DEFAULT_WING_PCT = 0.03
# Every CPPI selection the board offers is computed on each tick so each viewer can pick
# their own without changing anything on the server: (key, moneyness band, points from ATM).
# ("all", None, None) = every quoted strike of the 0DTE chain, no moneyness filter.
CPPI_VARIANTS: tuple[tuple[str, float | None, float | None], ...] = (
    ("all", None, None),
    ("p100", None, 100.0),
    ("p150", None, 150.0),
    ("p200", None, 200.0),
    ("p300", None, 300.0),
)
DEFAULT_HEADLINE_DTE = 0


def _dte(expiry, now: datetime) -> int:
    """DTE against the option session: today's expiry is -1 (gone) once 4pm NY has passed."""
    return days_to_expiry(expiry, now)


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
    otm_points: float | None = None,
) -> ExpirySlice:
    calls, puts = select_near_otm(quotes, spot, band, points=otm_points)
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


def _cppi_variant(
    quotes: list[OptionQuote],
    spot: float,
    band: float | None,
    points: float | None,
    rr: float | None,
) -> dict:
    """CPPI and premiums for one selection rule, plus the bias text it implies."""
    if points is None and band is None:
        calls = [q for q in quotes if q.right == "call" and q.mid and q.mid > 0]
        puts = [q for q in quotes if q.right == "put" and q.mid and q.mid > 0]
        span = strike_range(quotes)
        call_window = put_window = span
    else:
        b = band if band is not None else DEFAULT_BAND
        calls, puts = select_near_otm(quotes, spot, b, points=points)
        call_window = put_window = list(selection_window(spot, b, points))
    call_prem = volume_premium(calls)
    put_prem = volume_premium(puts)
    cppi = call_put_premium_imbalance(call_prem, put_prem)
    call_surge = volume_weighted_pct_change(calls)
    put_surge = volume_weighted_pct_change(puts)
    gap = call_surge - put_surge if call_surge is not None and put_surge is not None else None
    bias, score, summary_ko, _ = bias_from_metrics(cppi, rr, gap)
    return {
        "band": band,
        "points": points,
        "cppi": cppi,
        "call_premium": round(call_prem, 2),
        "put_premium": round(put_prem, 2),
        "call_volume": total_volume(calls),
        "put_volume": total_volume(puts),
        "call_strikes": strike_range(calls),
        "put_strikes": strike_range(puts),
        "call_window": call_window,
        "put_window": put_window,
        "call_count": len(calls),
        "put_count": len(puts),
        "bias": bias,
        "score": score,
        "summary_ko": summary_ko,
    }


def build_report(
    chain: OptionChain,
    max_dte: int = DEFAULT_HEADLINE_DTE,
    moneyness_band: float = DEFAULT_BAND,
    wing_pct: float = DEFAULT_WING_PCT,
    otm_points: float | None = None,
) -> SignalReport:
    if otm_points is not None and otm_points <= 0:
        otm_points = None
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
            _slice_for_expiry(expiry, quotes, chain.spot, now, moneyness_band, wing_pct, otm_points)
        )
        if dte <= max_dte:
            headline_all.extend(quotes)
            calls, puts = select_near_otm(quotes, chain.spot, moneyness_band, points=otm_points)
            headline_calls.extend(calls)
            headline_puts.extend(puts)

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
    variants = {
        key: _cppi_variant(headline_all, chain.spot, band, points, rr)
        for key, band, points in CPPI_VARIANTS
    }
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
        otm_points=otm_points,
        headline_call_strikes=strike_range(headline_calls),
        headline_put_strikes=strike_range(headline_puts),
        cppi_variants=variants,
        headline_expiry=nearest_expiry.isoformat() if nearest_expiry else None,
        headline_dte=_dte(nearest_expiry, now) if nearest_expiry else None,
        session_date=session_date(now).isoformat(),
    )


def _side(quote: OptionQuote, spot: float, t: float) -> dict:
    delta = quote.delta
    if delta is None:
        sigma = sane_iv(quote.iv)
        if sigma is not None:
            call = call_delta(spot, quote.strike, t, sigma)
            if call is not None:
                delta = call if quote.right == "call" else call - 1.0
    return {
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": quote.mid,
        "last": quote.last,
        "volume": int(quote.volume or 0),
        "open_interest": int(quote.open_interest or 0),
        "iv": sane_iv(quote.iv),
        "delta": None if delta is None else round(float(delta), 4),
        "delta_source": "feed" if quote.delta is not None else ("model" if delta is not None else None),
    }


def front_expiry_quotes(chain: OptionChain, max_dte: int = DEFAULT_HEADLINE_DTE):
    """(expiry, quotes) for the nearest expiry within max_dte, else the nearest living one."""
    now = chain.asof if chain.asof.tzinfo else chain.asof.replace(tzinfo=NY)
    grouped = _group_by_expiry(chain.quotes)
    for candidate in grouped:
        dte = _dte(candidate, now)
        if 0 <= dte <= max_dte:
            return candidate, grouped[candidate]
    living = [e for e in grouped if _dte(e, now) >= 0]
    if living:
        return living[0], grouped[living[0]]
    return None, []


def chain_table(chain: OptionChain, max_dte: int = DEFAULT_HEADLINE_DTE) -> dict:
    """Raw call/put quotes per strike for the nearest expiry within max_dte (0DTE by default)."""
    now = chain.asof if chain.asof.tzinfo else chain.asof.replace(tzinfo=NY)
    expiry, quotes = front_expiry_quotes(chain, max_dte)
    base = {
        "symbol": chain.symbol,
        "spot": chain.spot,
        "futures_price": chain.futures_price,
        "asof": now.astimezone(NY).isoformat(timespec="seconds"),
        "source": chain.source,
        "multiplier": chain.multiplier,
        "expiry": None,
        "dte": None,
        "atm": None,
        "rows": [],
    }
    if expiry is None:
        return base
    t = year_fraction(expiry, now=now)
    by_strike: dict[float, dict] = {}
    for quote in quotes:
        row = by_strike.setdefault(quote.strike, {"strike": quote.strike, "call": None, "put": None})
        row[quote.right] = _side(quote, chain.spot, t)
    rows = [by_strike[k] for k in sorted(by_strike)]
    atm = atm_strike(quotes, chain.spot)
    base.update({
        "expiry": expiry.isoformat(),
        "dte": _dte(expiry, now),
        "session_date": session_date(now).isoformat(),
        "atm": atm,
        "rows": rows,
    })
    return base


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def with_deltas(current: dict, prior_ticks: list[dict]) -> dict:
    """Attach 1-minute and 5-minute changes for scalping."""
    out = dict(current)
    out.setdefault("cppi_delta_1m", None)
    out.setdefault("cppi_delta_5m", None)
    out.setdefault("call_premium_delta_1m", None)
    out.setdefault("put_premium_delta_1m", None)
    out.setdefault("flow_1m", None)
    if not prior_ticks:
        return out

    prev = prior_ticks[-1]
    cur_cppi = current.get("headline_cppi")
    prev_cppi = prev.get("headline_cppi")
    if cur_cppi is not None and prev_cppi is not None:
        out["cppi_delta_1m"] = cur_cppi - prev_cppi

    d_call = float(current.get("headline_call_premium") or 0) - float(prev.get("headline_call_premium") or 0)
    d_put = float(current.get("headline_put_premium") or 0) - float(prev.get("headline_put_premium") or 0)
    out["call_premium_delta_1m"] = d_call
    out["put_premium_delta_1m"] = d_put
    total = abs(d_call) + abs(d_put)
    if total > 0:
        out["flow_1m"] = (d_call - d_put) / total

    now = _parse_ts(current.get("asof") or current.get("stored_at"))
    if now is None or cur_cppi is None:
        return out
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    target = now - timedelta(minutes=5)
    older = []
    for tick in prior_ticks:
        ts = _parse_ts(tick.get("asof") or tick.get("stored_at"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        if ts <= target:
            older.append(tick)
    if older and older[-1].get("headline_cppi") is not None:
        out["cppi_delta_5m"] = cur_cppi - older[-1]["headline_cppi"]
    return out


def yahoo_session_status(now: datetime | None = None) -> dict:
    """Yahoo option quotes are most trustworthy during US cash hours."""
    now = (now or datetime.now(tz=NY)).astimezone(NY)
    weekday = now.weekday()
    clock = now.time()
    if weekday >= 5:
        return {"code": "weekend", "label_ko": "주말 · 옵션 호가 멈춤", "live": False}
    if time(9, 30) <= clock < time(16, 0):
        return {"code": "rth", "label_ko": "정규장", "live": True}
    if clock < time(9, 30):
        return {"code": "pre", "label_ko": "장전 · 옵션 시세는 지연될 수 있음", "live": False}
    return {"code": "after", "label_ko": "장후 · 옵션 시세는 지연될 수 있음", "live": False}

