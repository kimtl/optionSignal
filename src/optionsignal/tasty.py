from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .metrics import option_mid, sane_iv
from .models import OptionChain, OptionQuote
from .settings import env_str

log = logging.getLogger("optionsignal")
NY = ZoneInfo("America/New_York")
DEFAULT_FUTURES_ROOT = "/NQ"
MAX_STREAM_CONTRACTS = 48


class FeedNotReady(RuntimeError):
    """DXLink is still connecting; the board should retry, not treat this as a dead proxy."""

    status_code = 503


class FeedConfigError(RuntimeError):
    """Symbol/source mismatch, e.g. /NQ without tastytrade credentials."""

    status_code = 400


def is_futures_root(symbol: str) -> bool:
    cleaned = symbol.strip().upper().lstrip("^")
    return cleaned.startswith("/") or cleaned in {"NQ", "MNQ", "ES", "MES"}


def product_code(symbol: str) -> str:
    cleaned = symbol.strip().upper().lstrip("/")
    return cleaned.split(":")[0] or "NQ"


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _right(option_type: object, streamer_symbol: str = "") -> str:
    """tastytrade uses OptionType.PUT = "P", not the word PUT."""
    raw = getattr(option_type, "value", option_type)
    text = str(raw or "").upper().strip()
    name = str(getattr(option_type, "name", "") or "").upper().strip()
    if text in {"P", "PUT"} or name in {"P", "PUT"} or text.endswith(".P"):
        return "put"
    if "PUT" in f"{name} {text}":
        return "put"
    if text in {"C", "CALL"} or name in {"C", "CALL"}:
        return "call"
    stream = (streamer_symbol or "").upper().replace(" ", "")
    match = re.search(r"([CP])\d+(?:\.\d+)?$", stream)
    if match:
        return "put" if match.group(1) == "P" else "call"
    return "call"


@dataclass
class LiveContract:
    expiry: date
    right: str
    strike: float
    streamer_symbol: str
    open_interest: int = 0
    occ_symbol: str = ""


def pick_front_future(items: list, today: date | None = None):
    """Pick the front-month future. `/NQ` is a product root, not a contract."""
    if not items:
        return None
    today = today or datetime.now(tz=NY).date()
    active = [item for item in items if getattr(item, "active_month", False)]
    pool = active or list(items)
    living = [item for item in pool if getattr(item, "expiration_date", date.max) >= today]
    pool = living or pool

    def key(item):
        return getattr(item, "expiration_date", date.max)

    return min(pool, key=key)


def nearest_contracts(contracts: list[LiveContract], spot: float, limit: int = MAX_STREAM_CONTRACTS) -> list[LiveContract]:
    if len(contracts) <= limit:
        return contracts
    unique = sorted({c.strike for c in contracts}, key=lambda strike: abs(strike - spot))
    keep = set(unique[: max(1, limit // 2)])
    return [c for c in contracts if c.strike in keep]


@dataclass
class StreamQuote:
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: float = 0.0
    ask_size: float = 0.0
    iv: float | None = None
    day_volume: int = 0
    delta: float | None = None


def snapshot_from_cache(
    contracts: list[LiveContract],
    quotes: dict[str, StreamQuote],
    *,
    symbol: str,
    spot: float,
    futures_price: float | None,
    multiplier: int = 1,
    now: datetime | None = None,
) -> OptionChain:
    now = now or datetime.now(tz=NY)
    quotes_out: list[OptionQuote] = []
    for contract in contracts:
        live = quotes.get(contract.streamer_symbol, StreamQuote())
        mid = option_mid(live.bid, live.ask, live.last)
        size = max(live.bid_size, 0) + max(live.ask_size, 0)
        volume = live.day_volume if live.day_volume > 0 else int(round(size))
        if volume <= 0 and mid:
            volume = 1
        quotes_out.append(
            OptionQuote(
                expiry=contract.expiry,
                right=contract.right,  # type: ignore[arg-type]
                strike=contract.strike,
                bid=live.bid,
                ask=live.ask,
                last=live.last,
                mid=mid,
                volume=max(volume, 0),
                open_interest=contract.open_interest,
                iv=sane_iv(live.iv),
                percent_change=None,
                delta=live.delta,
            )
        )
    return OptionChain(
        symbol=symbol.lstrip("/").upper(),
        spot=spot,
        futures_symbol="NQ=F",
        futures_price=futures_price,
        asof=now.astimezone(NY),
        quotes=quotes_out,
        multiplier=multiplier,
        source="tastytrade",
    )


class TastyFeed:
    """dxFeed quotes via tastytrade DXLink, with REST quotes so the board is not stuck waiting."""

    def __init__(self, symbol: str = DEFAULT_FUTURES_ROOT, max_dte: int = 0, band: float = 0.08) -> None:
        self.symbol = symbol
        self.max_dte = max(0, int(max_dte))
        self.band = band
        self.contracts: list[LiveContract] = []
        self.quotes: dict[str, StreamQuote] = {}
        self.underlying_symbol: str | None = None
        self.future_symbol: str | None = None
        self.phase = "idle"
        self.ready = False
        self.streaming = False
        self.error: str | None = None
        self._running = False
        self._spot: float | None = None

    @property
    def quoted_count(self) -> int:
        return sum(1 for c in self.contracts if c.streamer_symbol in self.quotes)

    def snapshot(self) -> OptionChain:
        if self.error and not self.contracts:
            raise RuntimeError(f"tastytrade 연결 실패: {self.error}")
        if not self.contracts:
            raise FeedNotReady(self.error or "tastytrade 체인을 불러오는 중입니다.")
        spot = self._spot
        if spot is None:
            strikes = sorted(c.strike for c in self.contracts)
            spot = strikes[len(strikes) // 2]
            self._spot = spot
        multiplier = 1 if is_futures_root(self.symbol) else 100
        return snapshot_from_cache(
            self.contracts,
            self.quotes,
            symbol=self.symbol,
            spot=spot,
            futures_price=spot if is_futures_root(self.symbol) else self._spot,
            multiplier=multiplier,
        )

    async def wait_ready(self, timeout: float = 45.0) -> None:
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout
        while not self.ready:
            if self.error and not self.contracts:
                raise RuntimeError(self.error)
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(self.error or "tastytrade 실시간 호가를 기다리다 시간 초과")
            await asyncio.sleep(0.2)

    async def run(self) -> None:
        import asyncio

        self._running = True
        while self._running:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                self._running = False
                raise
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    self._running = False
                    raise
                self.error = f"{type(exc).__name__}: {exc}"
                self.phase = "error"
                self.streaming = False
                log.exception("tastytrade feed error")
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False

    async def _run_once(self) -> None:
        import asyncio

        from tastytrade import DXLinkStreamer, Session
        from tastytrade.dxfeed import Greeks, Quote, Trade

        secret = env_str("TASTYTRADE_CLIENT_SECRET", "")
        refresh = env_str("TASTYTRADE_REFRESH_TOKEN", "")
        is_test = env_str("TASTYTRADE_IS_TEST", "false").lower() in {"1", "true", "yes"}
        self.phase = "login"
        log.info("tastytrade login (test=%s)", is_test)
        async with Session(secret, refresh, is_test=is_test, timeout=20.0) as session:
            self.phase = "chain"
            await self._load_instruments(session)
            await self._hydrate_quotes_rest(session)
            if self.contracts and self._spot:
                self.ready = True
                self.error = None
                self.phase = "quotes"
                log.info(
                    "tastytrade chain ready: %s contracts spot=%s future=%s",
                    len(self.contracts),
                    self._spot,
                    self.future_symbol,
                )
            try:
                await self._stream(session, DXLinkStreamer, Quote, Greeks, Trade)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.streaming = False
                self.error = f"DXLink: {exc}"
                log.exception("DXLink failed; falling back to REST quotes")
                while self._running:
                    await self._hydrate_quotes_rest(session)
                    self.phase = "quotes"
                    await asyncio.sleep(5)

    async def _stream(self, session, DXLinkStreamer, Quote, Greeks, Trade) -> None:
        import asyncio

        self.phase = "stream"
        symbols = [c.streamer_symbol for c in self.contracts]
        if self.underlying_symbol:
            symbols = [self.underlying_symbol, *symbols]
        log.info("DXLink subscribe %s symbols", len(symbols))
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, symbols)
            await streamer.subscribe(Greeks, [c.streamer_symbol for c in self.contracts])
            if self.underlying_symbol:
                await streamer.subscribe(Trade, [self.underlying_symbol])
            self.phase = "live"
            self.streaming = True
            self.error = None
            while self._running:
                drained = False
                while True:
                    event = streamer.get_event_nowait(Quote)
                    if event is None:
                        break
                    drained = True
                    self._apply_quote(event)
                while True:
                    event = streamer.get_event_nowait(Greeks)
                    if event is None:
                        break
                    drained = True
                    self._apply_greeks(event)
                while True:
                    event = streamer.get_event_nowait(Trade)
                    if event is None:
                        break
                    drained = True
                    self._apply_trade(event)
                if self._spot and self.quoted_count:
                    self.ready = True
                if not drained:
                    await asyncio.sleep(0.15)

    def _apply_quote(self, event) -> None:
        symbol = getattr(event, "event_symbol", None) or getattr(event, "eventSymbol", None)
        if not symbol:
            return
        live = self.quotes.setdefault(symbol, StreamQuote())
        live.bid = _num(getattr(event, "bid_price", None) or getattr(event, "bidPrice", None))
        live.ask = _num(getattr(event, "ask_price", None) or getattr(event, "askPrice", None))
        live.bid_size = _num(getattr(event, "bid_size", None) or getattr(event, "bidSize", None)) or 0.0
        live.ask_size = _num(getattr(event, "ask_size", None) or getattr(event, "askSize", None)) or 0.0
        if symbol == self.underlying_symbol:
            mid = option_mid(live.bid, live.ask, live.last)
            if mid:
                self._spot = mid

    def _apply_greeks(self, event) -> None:
        symbol = getattr(event, "event_symbol", None) or getattr(event, "eventSymbol", None)
        if not symbol:
            return
        live = self.quotes.setdefault(symbol, StreamQuote())
        live.iv = _num(getattr(event, "volatility", None))
        last = _num(getattr(event, "price", None))
        if last:
            live.last = last

    def _apply_trade(self, event) -> None:
        symbol = getattr(event, "event_symbol", None) or getattr(event, "eventSymbol", None)
        price = _num(getattr(event, "price", None))
        if symbol == self.underlying_symbol and price:
            self._spot = price
            live = self.quotes.setdefault(symbol, StreamQuote())
            live.last = price

    async def _load_instruments(self, session) -> None:
        from tastytrade.instruments import Future, get_future_option_chain, get_option_chain

        today = datetime.now(tz=NY).date()
        contracts: list[LiveContract] = []
        spot = None
        underlying = None
        if is_futures_root(self.symbol):
            root = product_code(self.symbol)
            future = await _front_future(Future, session, root)
            if future is not None:
                self.future_symbol = str(getattr(future, "symbol", "") or "")
                underlying = getattr(future, "streamer_symbol", None) or self.future_symbol
                spot = await _future_last(session, self.future_symbol)
                if spot is None:
                    spot = _num(getattr(future, "prev_daily_close", None)) or _num(getattr(future, "close", None))
                log.info("front future %s streamer=%s spot=%s", self.future_symbol, underlying, spot)
            chain = await get_future_option_chain(session, root)
        else:
            underlying = self.symbol.lstrip("^")
            chain = await get_option_chain(session, underlying)
        if not isinstance(chain, dict):
            raise RuntimeError("tastytrade 옵션 체인을 읽지 못했습니다.")
        expiries = sorted(chain)
        for expiry, options in sorted(chain.items()):
            for option in options:
                expiry_date = expiry if isinstance(expiry, date) else getattr(option, "expiration_date", None)
                if expiry_date is None:
                    continue
                dte_cal = (expiry_date - today).days
                dte_api = getattr(option, "days_to_expiration", None)
                dte = dte_cal if dte_api is None else min(dte_cal, int(dte_api))
                if dte < 0 or dte > self.max_dte:
                    continue
                strike = _num(getattr(option, "strike_price", None))
                streamer = getattr(option, "streamer_symbol", None)
                if strike is None or not streamer:
                    continue
                if spot and not (spot * (1 - self.band) <= strike <= spot * (1 + self.band)):
                    continue
                oi = int(_num(getattr(option, "open_interest", 0)) or 0)
                occ = str(getattr(option, "symbol", "") or "")
                contracts.append(
                    LiveContract(
                        expiry=expiry_date,
                        right=_right(getattr(option, "option_type", "C")),
                        strike=strike,
                        streamer_symbol=str(streamer),
                        open_interest=oi,
                        occ_symbol=str(getattr(option, "symbol", "") or ""),
                    )
                )
        if not contracts:
            upcoming = ", ".join(e.isoformat() for e in expiries[:6]) or "없음"
            zero_dte = [e for e in expiries if (e - today).days == 0]
            if not zero_dte:
                raise RuntimeError(
                    f"tastytrade에 오늘({today.isoformat()}) 0DTE 만기가 없습니다. "
                    f"남은 만기: {upcoming}"
                )
            raise RuntimeError(
                f"tastytrade에서 오늘({today.isoformat()}) 0DTE 스트라이크를 찾지 못했습니다. "
                "spot ±8% 안이 비었거나 NQ 선물옵션 시세 권한이 없을 수 있습니다. "
                f"체인 만기: {upcoming}"
            )
        if spot is None:
            strikes = sorted({c.strike for c in contracts})
            spot = strikes[len(strikes) // 2]
        self.contracts = nearest_contracts(contracts, spot)
        self.underlying_symbol = str(underlying) if underlying else None
        self._spot = spot

    async def _hydrate_quotes_rest(self, session) -> None:
        from tastytrade.market_data import get_market_data_by_type

        try:
            if self.future_symbol:
                rows = await get_market_data_by_type(session, futures=[self.future_symbol])
                for row in rows:
                    last = _num(getattr(row, "last", None) or getattr(row, "mark", None) or getattr(row, "mid", None))
                    if last:
                        self._spot = last
            symbols = [c.occ_symbol for c in self.contracts if c.occ_symbol]
            for i in range(0, len(symbols), 80):
                chunk = symbols[i : i + 80]
                if is_futures_root(self.symbol):
                    rows = await get_market_data_by_type(session, future_options=chunk)
                else:
                    rows = await get_market_data_by_type(session, options=chunk)
                by_symbol = {str(getattr(row, "symbol", "")): row for row in rows}
                for contract in self.contracts:
                    row = by_symbol.get(contract.occ_symbol)
                    if row is None:
                        continue
                    live = self.quotes.setdefault(contract.streamer_symbol, StreamQuote())
                    live.bid = _num(getattr(row, "bid", None))
                    live.ask = _num(getattr(row, "ask", None))
                    live.last = _num(getattr(row, "last", None) or getattr(row, "mark", None))
                    live.bid_size = _num(getattr(row, "bid_size", None)) or 0.0
                    live.ask_size = _num(getattr(row, "ask_size", None)) or 0.0
                    live.iv = sane_iv(_num(getattr(row, "implied_volatility", None)))
        except Exception as exc:  # noqa: BLE001
            log.warning("REST quotes failed: %s", exc)


async def _front_future(Future, session, root: str):
    try:
        items = await Future.get(session, product_codes=[root])
    except Exception as exc:  # noqa: BLE001
        log.warning("Future.get product_codes=%s failed: %s", root, exc)
        return None
    if items is None:
        return None
    if not isinstance(items, list):
        items = [items]
    return pick_front_future(items)


async def _future_last(session, symbol: str | None) -> float | None:
    if not symbol:
        return None
    try:
        from tastytrade.market_data import get_market_data_by_type

        rows = await get_market_data_by_type(session, futures=[symbol])
        for row in rows:
            last = _num(getattr(row, "last", None) or getattr(row, "mark", None) or getattr(row, "mid", None))
            if last:
                return last
    except Exception as exc:  # noqa: BLE001
        log.warning("future last price failed for %s: %s", symbol, exc)
    return None
