from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .metrics import option_mid, sane_iv
from .models import OptionChain, OptionQuote
from .settings import env_str

NY = ZoneInfo("America/New_York")
DEFAULT_FUTURES_ROOT = "/NQ"


def is_futures_root(symbol: str) -> bool:
    cleaned = symbol.strip().upper().lstrip("^")
    return cleaned.startswith("/") or cleaned in {"NQ", "MNQ", "ES", "MES"}


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


def _right(option_type: object) -> str:
    text = str(option_type).upper()
    return "put" if "PUT" in text or text.endswith(".P") or text.endswith(": P") else "call"


@dataclass
class LiveContract:
    expiry: date
    right: str
    strike: float
    streamer_symbol: str
    open_interest: int = 0


@dataclass
class StreamQuote:
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: float = 0.0
    ask_size: float = 0.0
    iv: float | None = None
    day_volume: int = 0


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
    """dxFeed quotes via tastytrade DXLink. Snapshot the in-memory book for the board."""

    def __init__(self, symbol: str = DEFAULT_FUTURES_ROOT, max_dte: int = 1, band: float = 0.08) -> None:
        self.symbol = symbol
        self.max_dte = max(0, int(max_dte))
        self.band = band
        self.contracts: list[LiveContract] = []
        self.quotes: dict[str, StreamQuote] = {}
        self.underlying_symbol: str | None = None
        self.ready = False
        self.error: str | None = None
        self._running = False
        self._spot: float | None = None

    def snapshot(self) -> OptionChain:
        if not self.contracts:
            raise RuntimeError("tastytrade 체인이 아직 없습니다.")
        spot = self._spot
        if spot is None:
            raise RuntimeError("tastytrade 기초자산 시세가 아직 없습니다.")
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
            if self.error:
                raise RuntimeError(self.error)
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("tastytrade 실시간 호가를 기다리다 시간 초과")
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
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                self.ready = False
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False

    async def _run_once(self) -> None:
        from tastytrade import DXLinkStreamer, Session
        from tastytrade.dxfeed import Greeks, Quote
        from tastytrade.instruments import Future, get_future_option_chain, get_option_chain

        secret = env_str("TASTYTRADE_CLIENT_SECRET", "")
        refresh = env_str("TASTYTRADE_REFRESH_TOKEN", "")
        is_test = env_str("TASTYTRADE_IS_TEST", "false").lower() in {"1", "true", "yes"}
        session = Session(secret, refresh, is_test=is_test)
        await self._load_instruments(session, Future, get_future_option_chain, get_option_chain)
        symbols = [c.streamer_symbol for c in self.contracts]
        if self.underlying_symbol:
            symbols = [self.underlying_symbol, *symbols]
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, symbols)
            await streamer.subscribe(Greeks, [c.streamer_symbol for c in self.contracts])
            import asyncio

            quote_task = asyncio.create_task(self._listen_quotes(streamer, Quote))
            greeks_task = asyncio.create_task(self._listen_greeks(streamer, Greeks))
            try:
                while self._running:
                    await asyncio.sleep(0.25)
                    quoted = sum(1 for c in self.contracts if c.streamer_symbol in self.quotes)
                    if self._spot and quoted >= max(4, len(self.contracts) // 8):
                        self.ready = True
                        self.error = None
            finally:
                quote_task.cancel()
                greeks_task.cancel()

    async def _listen_quotes(self, streamer, quote_cls) -> None:
        async for event in streamer.listen(quote_cls):
            symbol = getattr(event, "event_symbol", None) or getattr(event, "eventSymbol", None)
            if not symbol:
                continue
            live = self.quotes.setdefault(symbol, StreamQuote())
            live.bid = _num(getattr(event, "bid_price", None) or getattr(event, "bidPrice", None))
            live.ask = _num(getattr(event, "ask_price", None) or getattr(event, "askPrice", None))
            live.bid_size = _num(getattr(event, "bid_size", None) or getattr(event, "bidSize", None)) or 0.0
            live.ask_size = _num(getattr(event, "ask_size", None) or getattr(event, "askSize", None)) or 0.0
            if symbol == self.underlying_symbol:
                mid = option_mid(live.bid, live.ask, live.last)
                if mid:
                    self._spot = mid

    async def _listen_greeks(self, streamer, greeks_cls) -> None:
        async for event in streamer.listen(greeks_cls):
            symbol = getattr(event, "event_symbol", None) or getattr(event, "eventSymbol", None)
            if not symbol:
                continue
            live = self.quotes.setdefault(symbol, StreamQuote())
            live.iv = _num(getattr(event, "volatility", None))
            last = _num(getattr(event, "price", None))
            if last:
                live.last = last

    async def _load_instruments(self, session, Future, get_future_option_chain, get_option_chain) -> None:
        today = datetime.now(tz=NY).date()
        contracts: list[LiveContract] = []
        spot = None
        underlying = None
        if is_futures_root(self.symbol):
            root = self.symbol if self.symbol.startswith("/") else f"/{self.symbol}"
            future = await _front_future(Future, session, root)
            if future is not None:
                underlying = getattr(future, "streamer_symbol", None) or str(getattr(future, "symbol", root))
                spot = _num(getattr(future, "prev_daily_close", None)) or _num(getattr(future, "close", None))
            chain = await get_future_option_chain(session, root)
        else:
            underlying = self.symbol.lstrip("^")
            chain = await get_option_chain(session, underlying)
        if not isinstance(chain, dict):
            raise RuntimeError("tastytrade 옵션 체인을 읽지 못했습니다.")
        for expiry, options in sorted(chain.items()):
            dte = (expiry - today).days
            if dte < 0 or dte > self.max_dte:
                continue
            for option in options:
                strike = _num(getattr(option, "strike_price", None))
                streamer = getattr(option, "streamer_symbol", None)
                if strike is None or not streamer:
                    continue
                if spot and not (spot * (1 - self.band) <= strike <= spot * (1 + self.band)):
                    continue
                oi = int(_num(getattr(option, "open_interest", 0)) or 0)
                contracts.append(
                    LiveContract(
                        expiry=expiry,
                        right=_right(getattr(option, "option_type", "C")),
                        strike=strike,
                        streamer_symbol=str(streamer),
                        open_interest=oi,
                    )
                )
        if not contracts:
            raise RuntimeError("tastytrade에서 근월 스트라이크를 찾지 못했습니다.")
        if spot is None and len(contracts) > 160:
            contracts = contracts[:160]
        self.contracts = contracts
        self.underlying_symbol = underlying
        if spot:
            self._spot = spot


async def _front_future(Future, session, root: str):
    try:
        items = await Future.get(session, root)
    except Exception:
        return None
    if items is None:
        return None
    if not isinstance(items, list):
        items = [items]
    if not items:
        return None
    def exp(item):
        return getattr(item, "expiration_date", date.max)
    return min(items, key=exp)
