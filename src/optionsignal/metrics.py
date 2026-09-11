from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import OptionQuote, Right

NY = ZoneInfo("America/New_York")

# Index/ETF options can print absurd IVs on far OTM 0DTE quotes.
IV_MIN = 0.03
IV_MAX = 2.5


def option_mid(bid: float | None, ask: float | None, last: float | None) -> float | None:
    """Use the bid/ask midpoint when a real market exists, otherwise last."""
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    if last is not None and last > 0:
        return float(last)
    return None


def sane_iv(iv: float | None) -> float | None:
    if iv is None or not math.isfinite(iv):
        return None
    if iv < IV_MIN or iv > IV_MAX:
        return None
    return float(iv)


EXPIRY_TIME = time(16, 0)  # NQ weekly/daily options and QQQ/NDX expire 4:00pm New York


def _ny(now: datetime | None) -> datetime:
    now = now or datetime.now(tz=NY)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    return now.astimezone(NY)


def session_date(now: datetime | None = None) -> date:
    """The date whose expiry counts as 0DTE right now.

    Until 4:00pm New York that is today; from 4:00pm on, today's contracts have
    expired and the next calendar day becomes the "0DTE" session (Friday evening
    rolls to Saturday, which has no expiry, so the nearest living expiry is Monday).
    """
    now = _ny(now)
    if now.time() >= EXPIRY_TIME:
        return now.date() + timedelta(days=1)
    return now.date()


def days_to_expiry(expiry: date, now: datetime | None = None) -> int:
    """Calendar DTE relative to the current session; -1 means already expired."""
    return (expiry - session_date(now)).days


def expiry_datetime(expiry: date) -> datetime:
    return datetime.combine(expiry, EXPIRY_TIME, tzinfo=NY)


def year_fraction(expiry: date, now: datetime | None = None) -> float:
    """Time to 4pm New York expiry, floored at 30 minutes so 0DTE greeks don't explode."""
    now = _ny(now)
    expiry_dt = expiry_datetime(expiry)
    seconds = (expiry_dt - now).total_seconds()
    seconds = max(seconds, 30 * 60)
    return seconds / (365.25 * 24 * 3600)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def call_delta(spot: float, strike: float, t: float, sigma: float, rate: float = 0.0) -> float | None:
    if spot <= 0 or strike <= 0 or t <= 0 or sigma is None or sigma <= 0:
        return None
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return _norm_cdf(d1)


def call_put_premium_imbalance(call_premium: float, put_premium: float) -> float | None:
    """(C - P) / (C + P). +1 = only calls, -1 = only puts, 0 = balanced."""
    total = call_premium + put_premium
    if total <= 0:
        return None
    return (call_premium - put_premium) / total


def premium_ratio(call_premium: float, put_premium: float) -> float | None:
    if put_premium <= 0:
        return None if call_premium <= 0 else math.inf
    return call_premium / put_premium


def weighted_mean(values: Iterable[float], weights: Iterable[float]) -> float | None:
    num = 0.0
    den = 0.0
    for value, weight in zip(values, weights, strict=True):
        if weight <= 0 or not math.isfinite(value):
            continue
        num += value * weight
        den += weight
    if den <= 0:
        return None
    return num / den


def in_moneyness_band(strike: float, spot: float, band: float) -> bool:
    if spot <= 0:
        return False
    return (1.0 - band) * spot <= strike <= (1.0 + band) * spot


def is_otm(quote: OptionQuote, spot: float, atm: float | None) -> bool:
    """Strictly out-of-the-money and not the ATM strike: calls above spot, puts below.

    ITM and ATM quotes are excluded from CPPI so that call and put premium are
    compared on mirror-image strike sets (spot 100 → calls 101+, puts 99-).
    """
    if quote.mid is None or quote.mid <= 0 or spot <= 0:
        return False
    if atm is not None and quote.strike == atm:
        return False
    if quote.right == "call":
        return quote.strike > spot
    return quote.strike < spot


def is_near_otm(quote: OptionQuote, spot: float, band: float, atm: float | None = None) -> bool:
    """OTM (not ATM) quotes within ``band`` percent of spot."""
    return is_otm(quote, spot, atm) and in_moneyness_band(quote.strike, spot, band)


def atm_strike(quotes: Iterable[OptionQuote], spot: float) -> float | None:
    strikes = [q.strike for q in quotes if q.strike > 0]
    if not strikes or spot <= 0:
        return None
    return min(strikes, key=lambda k: abs(k - spot))


def select_near_otm(
    quotes: Iterable[OptionQuote],
    spot: float,
    band: float,
    points: float | None = None,
) -> tuple[list[OptionQuote], list[OptionQuote]]:
    """Pick the call and put sides used for CPPI: OTM only, mirror-image windows.

    points=None: OTM strikes within ``band`` percent of spot.
    points=N: OTM strikes out to N index points, so 100/150/200 points on NQ
    mean the same thing on every trading day.
    Spot 100, N=20 → calls 101~120, puts 80~99. The ATM strike and every ITM
    quote are left out on both sides.
    """
    quotes = list(quotes)
    atm = atm_strike(quotes, spot)
    (c_lo, c_hi), (p_lo, p_hi) = otm_windows(spot, band, points)
    calls = [q for q in quotes if q.right == "call" and is_otm(q, spot, atm) and c_lo < q.strike <= c_hi]
    puts = [q for q in quotes if q.right == "put" and is_otm(q, spot, atm) and p_lo <= q.strike < p_hi]
    return calls, puts


def select_all_otm(
    quotes: Iterable[OptionQuote],
    spot: float,
) -> tuple[list[OptionQuote], list[OptionQuote]]:
    """Every quoted OTM call and put of the chain (ATM/ITM excluded), no distance cap."""
    quotes = list(quotes)
    atm = atm_strike(quotes, spot)
    calls = [q for q in quotes if q.right == "call" and is_otm(q, spot, atm)]
    puts = [q for q in quotes if q.right == "put" and is_otm(q, spot, atm)]
    return calls, puts


def otm_windows(
    spot: float, band: float, points: float | None = None
) -> tuple[tuple[float, float], tuple[float, float]]:
    """(call_window, put_window) a CPPI rule covers: (spot, spot+N] and [spot-N, spot).

    The spot-side edge is exclusive; the two windows are mirror images.
    """
    if points is None or points <= 0:
        return (spot, spot * (1 + band)), (spot * (1 - band), spot)
    return (spot, spot + points), (spot - points, spot)


def selection_window(spot: float, band: float, points: float | None = None) -> tuple[float, float]:
    """Whole [low, high] strike span a CPPI rule covers across both sides."""
    (_, hi), (lo, _) = otm_windows(spot, band, points)
    return lo, hi


def strike_range(quotes: Iterable[OptionQuote]) -> list[float] | None:
    strikes = [q.strike for q in quotes]
    if not strikes:
        return None
    return [min(strikes), max(strikes)]


def trade_price(quote: OptionQuote) -> float | None:
    """Last traded price; falls back to the midpoint when the contract has not printed yet."""
    if quote.last is not None and quote.last > 0:
        return float(quote.last)
    if quote.mid is not None and quote.mid > 0:
        return float(quote.mid)
    return None


def quote_price(quote: OptionQuote, price: str = "mid") -> float | None:
    """Price used for premium flow: "mid" (bid/ask midpoint) or "last" (traded price)."""
    if price == "last":
        return trade_price(quote)
    if quote.mid is not None and quote.mid > 0:
        return float(quote.mid)
    return None


def volume_premium(quotes: Iterable[OptionQuote], price: str = "mid") -> float:
    total = 0.0
    for quote in quotes:
        value = quote_price(quote, price)
        if value is None:
            continue
        total += value * max(quote.volume, 0)
    return total


def total_volume(quotes: Iterable[OptionQuote]) -> int:
    return int(sum(max(q.volume, 0) for q in quotes))


def volume_weighted_pct_change(quotes: Iterable[OptionQuote]) -> float | None:
    values: list[float] = []
    weights: list[float] = []
    for quote in quotes:
        if quote.percent_change is None or not math.isfinite(quote.percent_change):
            continue
        if quote.volume <= 0:
            continue
        values.append(quote.percent_change)
        weights.append(float(quote.volume))
    return weighted_mean(values, weights)


def volume_weighted_iv(quotes: Iterable[OptionQuote]) -> float | None:
    values: list[float] = []
    weights: list[float] = []
    for quote in quotes:
        iv = sane_iv(quote.iv)
        if iv is None:
            continue
        weight = float(quote.volume) if quote.volume > 0 else 0.0
        # Fall back to open interest so a quiet wing still informs skew.
        if weight <= 0:
            weight = float(max(quote.open_interest, 0))
        if weight <= 0 and quote.mid:
            weight = 1.0
        values.append(iv)
        weights.append(weight)
    return weighted_mean(values, weights)


def nearest_quote(quotes: list[OptionQuote], strike: float) -> OptionQuote | None:
    valid = [q for q in quotes if q.mid is not None and q.mid > 0]
    if not valid:
        return None
    return min(valid, key=lambda q: abs(q.strike - strike))


def interpolate(xs: list[float], ys: list[float], x: float) -> float | None:
    if not xs or len(xs) != len(ys):
        return None
    paired = sorted(zip(xs, ys), key=lambda p: p[0])
    xs, ys = [p[0] for p in paired], [p[1] for p in paired]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if xs[i] >= x:
            span = xs[i] - xs[i - 1]
            if span <= 0:
                return ys[i]
            t = (x - xs[i - 1]) / span
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def iv_at_abs_delta(
    quotes: Iterable[OptionQuote],
    spot: float,
    expiry: date,
    right: Right,
    target: float = 0.25,
    now: datetime | None = None,
) -> float | None:
    """Linearly interpolate IV at |delta| ≈ target (default 25-delta)."""
    t = year_fraction(expiry, now=now)
    deltas: list[float] = []
    ivs: list[float] = []
    for quote in quotes:
        if quote.right != right:
            continue
        sigma = sane_iv(quote.iv)
        if sigma is None:
            continue
        delta = call_delta(spot, quote.strike, t, sigma)
        if delta is None:
            continue
        abs_delta = delta if right == "call" else abs(delta - 1.0)
        if abs_delta <= 0.01 or abs_delta >= 0.99:
            continue
        deltas.append(abs_delta)
        ivs.append(sigma)
    if len(deltas) < 2:
        return volume_weighted_iv([q for q in quotes if q.right == right])
    return interpolate(deltas, ivs, target)


def risk_reversal_iv(
    quotes: Iterable[OptionQuote],
    spot: float,
    expiry: date,
    now: datetime | None = None,
    target: float = 0.25,
) -> float | None:
    """25-delta call IV minus 25-delta put IV. Positive = calls expensive vs puts."""
    quotes = list(quotes)
    call_iv = iv_at_abs_delta(quotes, spot, expiry, "call", target=target, now=now)
    put_iv = iv_at_abs_delta(quotes, spot, expiry, "put", target=target, now=now)
    if call_iv is None or put_iv is None:
        return None
    return call_iv - put_iv


def bias_from_metrics(
    cppi: float | None,
    risk_reversal: float | None,
    surge_gap: float | None,
) -> tuple[str, int, str, str]:
    """Map metrics to a readable bias. Score is -2..+2, not a trade recommendation."""
    score = 0
    if cppi is not None:
        if cppi >= 0.25:
            score += 2
        elif cppi >= 0.08:
            score += 1
        elif cppi <= -0.25:
            score -= 2
        elif cppi <= -0.08:
            score -= 1
    if risk_reversal is not None:
        if risk_reversal >= 0.03:
            score += 1
        elif risk_reversal <= -0.03:
            score -= 1
    if surge_gap is not None:
        if surge_gap >= 8:
            score += 1
        elif surge_gap <= -8:
            score -= 1
    score = max(-2, min(2, score))

    labels = {
        2: (
            "call",
            "0DTE 콜 프리미엄·수요가 풋을 뚜렷하게 앞섭니다. 상승 베팅 자금이 몰리는 구간입니다.",
            "Near-term call premium/demand is clearly ahead of puts. Upside bets are being paid up.",
        ),
        1: (
            "mild_call",
            "콜 쪽으로 기울었습니다. 급등 강도는 아직 강하지 않습니다.",
            "Skewed toward calls, but the surge is not extreme yet.",
        ),
        0: (
            "neutral",
            "콜과 풋 시세가 크게 갈리지 않습니다. 한쪽만의 급등 신호는 약합니다.",
            "Call and put pricing are not far apart. No one-sided surge.",
        ),
        -1: (
            "mild_put",
            "풋 쪽으로 기울었습니다. 하락 헤지/베팅 수요가 조금 더 큽니다.",
            "Skewed toward puts. Downside hedges/bets are a bit more expensive.",
        ),
        -2: (
            "put",
            "0DTE 풋 프리미엄·수요가 콜을 뚜렷하게 앞섭니다. 하락 방어/베팅 자금이 몰리는 구간입니다.",
            "Near-term put premium/demand is clearly ahead of calls. Downside protection is being paid up.",
        ),
    }
    bias, summary_ko, summary_en = labels[score]
    return bias, score, summary_ko, summary_en
