from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from .metrics import option_mid, sane_iv, session_date
from .models import OptionChain, OptionQuote

NY = ZoneInfo("America/New_York")

DEFAULT_SYMBOL = "QQQ"
FUTURES_SYMBOL = "NQ=F"
MULTIPLIERS = {"QQQ": 100, "NDX": 100, "^NDX": 100, "SPY": 100}


def _to_float(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number) or number != number:
        return None
    return number


def _to_int(value) -> int:
    number = _to_float(value)
    if number is None:
        return 0
    return int(number)


def _last_price(ticker: yf.Ticker) -> float:
    try:
        info = ticker.fast_info
        price = getattr(info, "last_price", None)
        if price is None and hasattr(info, "get"):
            price = info.get("last_price")
        if price:
            return float(price)
    except Exception:
        pass
    history = ticker.history(period="1d")
    if history.empty:
        raise RuntimeError("Could not read last price")
    return float(history["Close"].iloc[-1])


def _quotes_from_frame(frame: pd.DataFrame, expiry: date, right: str) -> list[OptionQuote]:
    quotes: list[OptionQuote] = []
    if frame is None or frame.empty:
        return quotes
    for row in frame.itertuples(index=False):
        bid = _to_float(getattr(row, "bid", None))
        ask = _to_float(getattr(row, "ask", None))
        last = _to_float(getattr(row, "lastPrice", None))
        iv = sane_iv(_to_float(getattr(row, "impliedVolatility", None)))
        quotes.append(
            OptionQuote(
                expiry=expiry,
                right=right,  # type: ignore[arg-type]
                strike=float(row.strike),
                bid=bid,
                ask=ask,
                last=last,
                mid=option_mid(bid, ask, last),
                volume=_to_int(getattr(row, "volume", 0)),
                open_interest=_to_int(getattr(row, "openInterest", 0)),
                iv=iv,
                percent_change=_to_float(getattr(row, "percentChange", None)),
            )
        )
    return quotes


def list_expiries(symbol: str = DEFAULT_SYMBOL) -> list[str]:
    ticker = yf.Ticker(symbol)
    return list(ticker.options)


def fetch_chain(
    symbol: str = DEFAULT_SYMBOL,
    max_dte: int = 0,
    expiry_limit: int = 8,
    now: datetime | None = None,
) -> OptionChain:
    """Pull Yahoo option chains for a Nasdaq-100 proxy (QQQ / ^NDX).

    True CME NQ futures option quotes are not on Yahoo. QQQ and NDX options
    are the liquid public proxy for the same index; NQ=F is fetched as the
    futures reference price.

    Scalping polls should keep max_dte at 0 so only today's expiry is used.
    """
    from .tasty import FeedConfigError, is_futures_root

    if is_futures_root(symbol):
        raise FeedConfigError(
            f"{symbol} 선물옵션 체인은 Yahoo에 없습니다. "
            "NQ 실시간은 TASTYTRADE_CLIENT_SECRET과 TASTYTRADE_REFRESH_TOKEN이 필요합니다. "
            "지연 시세만 쓰려면 심볼을 QQQ로 바꾸세요."
        )

    now = now or datetime.now(tz=NY)
    # From 4pm New York today's expiry is dead; the next session's expiry is the 0DTE one.
    today = session_date(now)
    ticker = yf.Ticker(symbol)
    spot = _last_price(ticker)

    futures_price: float | None = None
    try:
        futures_price = _last_price(yf.Ticker(FUTURES_SYMBOL))
    except Exception:
        futures_price = None

    quotes: list[OptionQuote] = []
    taken = 0
    for expiry_str in ticker.options:
        expiry = date.fromisoformat(expiry_str)
        dte = (expiry - today).days
        if dte < 0:
            continue
        if dte > max_dte:
            break
        chain = ticker.option_chain(expiry_str)
        quotes.extend(_quotes_from_frame(chain.calls, expiry, "call"))
        quotes.extend(_quotes_from_frame(chain.puts, expiry, "put"))
        taken += 1
        if taken >= expiry_limit:
            break

    if not quotes:
        expiries = ", ".join(list(ticker.options)[:6]) or "없음"
        raise RuntimeError(
            f"{symbol} 0DTE 옵션을 못 읽었습니다 (세션 {today.isoformat()}, DTE≤{max_dte}). "
            f"Yahoo 만기: {expiries}. 주말이거나 Yahoo가 이 서버 IP를 막았을 수 있습니다."
        )

    return OptionChain(
        symbol=symbol.upper().lstrip("^"),
        spot=spot,
        futures_symbol=FUTURES_SYMBOL,
        futures_price=futures_price,
        asof=now.astimezone(NY),
        quotes=quotes,
        multiplier=MULTIPLIERS.get(symbol.upper(), 100),
        source="yahoo",
    )


def fetch_price_history(symbol: str = FUTURES_SYMBOL, hours: int = 12) -> list[dict]:
    """Yahoo 1-minute candles for the last `hours` (delayed; used when there is no tastytrade).

    Futures roots (/NQ, NQ) map to NQ=F. Returns [{asof, open, high, low, close, volume}].
    """
    yahoo = symbol.upper()
    if yahoo.lstrip("/") in {"NQ", "MNQ"}:
        yahoo = FUTURES_SYMBOL
    # Yahoo's "1d"/"2d" windows start at midnight New York, so an overnight futures
    # session is cut off. "5d" returns the full minute history (1m allows up to 7d).
    frame = yf.Ticker(yahoo).history(period="5d", interval="1m", prepost=True, auto_adjust=False)
    if frame is None or frame.empty:
        return []
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours)
    out: list[dict] = []
    for ts, row in frame.iterrows():
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        if stamp < cutoff:
            continue
        close = _to_float(row.get("Close"))
        if close is None or close <= 0:
            continue
        out.append({
            "asof": stamp.tz_convert("UTC").isoformat(timespec="seconds"),
            "open": _to_float(row.get("Open")) or close,
            "high": _to_float(row.get("High")) or close,
            "low": _to_float(row.get("Low")) or close,
            "close": close,
            "volume": _to_int(row.get("Volume")),
        })
    return out
