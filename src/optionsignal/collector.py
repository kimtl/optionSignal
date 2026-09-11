from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .fetch import DEFAULT_SYMBOL, fetch_chain, fetch_price_history
from .metrics import session_date
from .settings import default_interval, public_url, tasty_configured
from .signal import (
    DEFAULT_BAND,
    DEFAULT_HEADLINE_DTE,
    build_report,
    chain_table,
    front_expiry_quotes,
    with_deltas,
    yahoo_session_status,
)
from .store import (
    BAR_HISTORY_LIMIT,
    CHART_HOURS,
    FUTURES_KEY,
    RAW_HISTORY_LIMIT,
    aggregate_bars,
    backfill_option_bars,
    compact_point,
    futures_history_points,
    load_history,
    load_option_rows,
    load_option_series,
    option_key,
    premium_ratio_points,
    price_bars,
    save_bars,
    save_option_ticks,
    save_snapshot,
)
from .tasty import (
    FUTURES_INFO,
    FeedConfigError,
    FeedNotReady,
    TastyFeed,
    is_futures_root,
    product_code,
    yahoo_futures_symbol,
)

log = logging.getLogger("optionsignal")

FetchFn = Callable[..., Any]
DEFAULT_INTERVAL = 60
FEED_BAND = 0.08  # strikes streamed from tastytrade: wider than any CPPI band so every variant has quotes


def lan_ip() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return None


class LiveHub:
    """Shared board. Yahoo polls, or tastytrade DXLink streams into memory."""

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        max_dte: int = DEFAULT_HEADLINE_DTE,
        band: float = DEFAULT_BAND,
        interval: int | None = None,
        fetch_fn: FetchFn | None = None,
        tasty_feed: TastyFeed | None = None,
        otm_points: float | None = None,
    ) -> None:
        self.symbol = symbol
        self.max_dte = max_dte
        self.band = band
        self.otm_points = otm_points if otm_points and otm_points > 0 else None
        self.interval = max(1, int(interval if interval is not None else default_interval()))
        self.fetch_fn = fetch_fn or fetch_chain
        self.tasty_feed = tasty_feed
        self.latest: dict | None = None
        self.latest_chain = None
        self.backfill: dict | None = None
        self.front_expiry = None  # expiry date currently used as 0DTE
        self._backfill_expiry = None
        self.error: str | None = None
        self.last_tick_at: datetime | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self._running = False
        self._tasty_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self.port = 8000

    def source_name(self) -> str:
        if self.tasty_feed is not None or tasty_configured():
            return "tastytrade"
        return "yahoo"

    def status(self) -> dict:
        url = public_url()
        source = self.source_name()
        feed = self.tasty_feed
        streaming = bool(feed is not None and getattr(feed, "streaming", False))
        ready = bool(feed is not None and feed.ready)
        realtime = source == "tastytrade" and streaming
        if realtime:
            session = {
                "code": "tasty",
                "label_ko": "tastytrade DXLink 실시간",
                "live": True,
            }
        elif source == "tastytrade" and ready:
            session = {
                "code": "tasty-rest",
                "label_ko": "tastytrade 시세 (REST)",
                "live": True,
            }
        elif source == "tastytrade":
            session = {
                "code": "tasty-wait",
                "label_ko": "tastytrade 연결 중",
                "live": False,
            }
        else:
            session = yahoo_session_status()
        feed_error = getattr(feed, "error", None) if feed else None
        stream_error = getattr(feed, "stream_error", None) if feed else None
        if ready and not realtime and stream_error:
            session["label_ko"] = "tastytrade 시세 (REST · DXLink 재연결 대기)"
        return {
            "symbol": self.symbol,
            "max_dte": self.max_dte,
            "otm_points": self.otm_points,
            "band": self.band,
            "interval": self.interval,
            "viewers": len(self.subscribers),
            "last_tick_at": self.last_tick_at.isoformat(timespec="seconds") if self.last_tick_at else None,
            "error": self.error or feed_error,
            "stream_error": stream_error,
            "stream_retries": int(getattr(feed, "stream_retries", 0) or 0) if feed else 0,
            "session": session,
            "source": source,
            "realtime": realtime,
            "ready": ready,
            "phase": getattr(feed, "phase", None) if feed else None,
            "contracts": len(getattr(feed, "contracts", []) or []) if feed else 0,
            "quoted": int(getattr(feed, "quoted_count", 0) or 0) if feed else 0,
            "backfill": self.backfill,
            "expiry": self.front_expiry.isoformat() if self.front_expiry else None,
            "session_date": session_date().isoformat(),
            "futures": self.futures_info(),
            "share_hint": url or lan_ip(),
            "public_url": url,
            "port": self.port,
        }

    def history_points(self) -> list[dict]:
        """Backfilled candles (before the server started) plus live ticks, oldest first."""
        symbol = self.symbol.lstrip("/")
        return futures_history_points(symbol) + load_history(symbol, limit=RAW_HISTORY_LIMIT)

    def live_payload(self) -> dict:
        bars = aggregate_bars(self.history_points(), minutes=1)
        points = [compact_point(item) for item in bars[-BAR_HISTORY_LIMIT:]]
        return {
            "tick": self.latest,
            "points": points,
            "status": self.status(),
        }

    async def backfill_history(self, hours: int = CHART_HOURS) -> dict:
        """Fill the chart with the hours before this process started.

        tastytrade: 1m dxFeed candles for the future and every streamed 0DTE contract.
        Otherwise: Yahoo 1m candles for the future only.
        """
        symbol = self.symbol.lstrip("/")
        summary = {"source": None, "nq_bars": 0, "options": 0}
        feed = self.tasty_feed
        if feed is not None:
            for _ in range(120):  # wait up to ~60s for the chain to load
                if feed.contracts and feed.underlying_symbol and getattr(feed, "_session", None) is not None:
                    break
                if not self._running:
                    return summary
                await asyncio.sleep(0.5)
            else:
                log.warning("backfill skipped: tastytrade chain not ready")
                return summary
            self._backfill_expiry = getattr(feed, "expiry", None)
            try:
                candles = await feed.fetch_candles(hours=hours)
            except Exception as exc:  # noqa: BLE001
                log.warning("dxFeed candle backfill failed: %s", exc)
                candles = {}
            summary["source"] = "tastytrade"
            summary["expiry"] = self._backfill_expiry.isoformat() if self._backfill_expiry else None
            nq = candles.get(feed.underlying_symbol) or []
            if nq:
                summary["nq_bars"] = await asyncio.to_thread(save_bars, symbol, FUTURES_KEY, nq)
            for contract in feed.contracts:
                bars = candles.get(contract.streamer_symbol)
                if not bars:
                    continue
                key = option_key(contract.strike, contract.right)
                await asyncio.to_thread(backfill_option_bars, symbol, key, bars, contract.expiry.isoformat())
                summary["options"] += 1
        else:
            summary["source"] = "yahoo"
            try:
                # The board's candles are the future behind the symbol (/ES -> ES=F, QQQ -> NQ=F).
                bars = await asyncio.to_thread(fetch_price_history, yahoo_futures_symbol(self.symbol), hours)
            except Exception as exc:  # noqa: BLE001
                log.warning("Yahoo history backfill failed: %s", exc)
                bars = []
            if bars:
                summary["nq_bars"] = await asyncio.to_thread(save_bars, symbol, FUTURES_KEY, bars)
        self.backfill = summary
        log.info("history backfill: %s", summary)
        if summary["nq_bars"] or summary["options"]:
            await self.broadcast(self.live_payload() | {"event": "tick"})
        return summary

    def option_series(self, keys: list[str], tf: int = 1, limit: int = BAR_HISTORY_LIMIT) -> dict:
        """OHLC bars of the mid price for each requested contract key (e.g. 24700C)."""
        symbol = self.symbol.lstrip("/").lstrip("^")
        series = {}
        expiry = self.front_expiry.isoformat() if self.front_expiry else None
        for key in keys[:8]:
            # Only the current expiry: after the 4pm roll, yesterday's 24700C is a different contract.
            raw = load_option_series(symbol, key, limit=RAW_HISTORY_LIMIT, expiry=expiry)
            bars = price_bars(raw, minutes=tf)
            latest = raw[-1] if raw else None
            series[key] = {"bars": bars[-limit:], "latest": latest}
        return {"symbol": self.symbol, "tf": tf, "expiry": expiry, "series": series}

    def premium_ratio_series(self, calls: list[str], puts: list[str], tf: int = 1, limit: int = BAR_HISTORY_LIMIT) -> dict:
        """Call ÷ put Σ(mid × volume) × 100 for the chosen contracts, as OHLC bars."""
        symbol = self.symbol.lstrip("/").lstrip("^")
        expiry = self.front_expiry.isoformat() if self.front_expiry else None
        calls = calls[:8]
        puts = puts[:8]
        rows = load_option_rows(symbol, calls + puts, expiry=expiry)
        pts = premium_ratio_points(rows, calls, puts)
        bars = price_bars(pts, minutes=tf, value="ratio")
        latest = pts[-1] if pts else None
        return {"symbol": self.symbol, "tf": tf, "expiry": expiry, "calls": calls, "puts": puts,
                "bars": bars[-limit:], "latest": latest}

    def futures_info(self) -> dict:
        code = product_code(self.symbol) if is_futures_root(self.symbol) else "NQ"
        info = FUTURES_INFO.get(code, FUTURES_INFO["NQ"])
        return {"code": code, "name_ko": info["name_ko"], "yahoo": info["yahoo"], "multiplier": info["multiplier"]}

    def chain_payload(self) -> dict:
        """Raw 0DTE call/put quotes for the chain tab. Uses the live cache when streaming."""
        chain = self.latest_chain
        feed = self.tasty_feed
        if feed is not None and feed.contracts:
            try:
                chain = feed.snapshot()
            except Exception:  # noqa: BLE001
                pass
        if chain is None:
            return {"symbol": self.symbol, "rows": [], "expiry": None, "spot": None, "asof": None,
                    "error": self.error or "아직 체인을 받지 못했습니다."}
        return chain_table(chain, max_dte=self.max_dte)

    def collect_once(self) -> dict:
        history = load_history(self.symbol.lstrip("/"), limit=240)
        chain = self._load_chain()
        report = build_report(
            chain, max_dte=self.max_dte, moneyness_band=self.band, otm_points=self.otm_points
        )
        payload = with_deltas(report.to_dict(), history)
        save_snapshot(payload)
        expiry, front = front_expiry_quotes(chain, self.max_dte)
        self.front_expiry = expiry
        try:
            save_option_ticks(chain.symbol, front)
        except Exception:  # noqa: BLE001
            log.exception("option tick save failed")
        self.latest = payload
        self.latest_chain = chain
        self.error = None
        self.last_tick_at = datetime.now(timezone.utc)
        return payload

    def _load_chain(self):
        if is_futures_root(self.symbol):
            if self.tasty_feed is None:
                if tasty_configured():
                    raise FeedNotReady("tastytrade 연결을 시작하는 중입니다. 몇 초 뒤 보드가 자동으로 찍습니다.")
                raise FeedConfigError(
                    f"{self.symbol} 선물옵션은 Yahoo에 없습니다. "
                    "Railway Variables에 TASTYTRADE_CLIENT_SECRET과 "
                    "TASTYTRADE_REFRESH_TOKEN을 넣거나, 심볼을 QQQ로 바꾸세요."
                )
            return self.tasty_feed.snapshot()
        return self.fetch_fn(
            symbol=self.symbol,
            max_dte=self.max_dte,
            expiry_limit=max(1, self.max_dte + 1),
        )

    async def broadcast(self, payload: dict) -> None:
        message = json.dumps(payload, ensure_ascii=False)
        dead: list[asyncio.Queue] = []
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.subscribers.discard(queue)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    async def _stop_task(self, task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def ensure_feed(self) -> bool:
        """Start, or swap, the tastytrade feed so it streams the board's current symbol.

        Returns True when the feed was (re)started, i.e. quotes are not ready yet.
        """
        if not tasty_configured():
            return False
        want = product_code(self.symbol) if is_futures_root(self.symbol) else None
        feed = self.tasty_feed
        have = product_code(feed.symbol) if feed is not None else None
        if want == have:
            return False
        await self._stop_task(self._tasty_task)
        await self._stop_task(self._backfill_task)
        if feed is not None:
            feed.stop()
        self.tasty_feed = None
        self.latest = None
        self.latest_chain = None
        self.front_expiry = None
        self.backfill = None
        self._backfill_expiry = None
        self.error = None
        if want:
            self.tasty_feed = TastyFeed(symbol=self.symbol, max_dte=self.max_dte, band=max(self.band, FEED_BAND))
            self._tasty_task = asyncio.create_task(self.tasty_feed.run(), name="tasty-dxlink")
            log.info("feed switched to %s", self.symbol)
        self._backfill_task = asyncio.create_task(self.backfill_history(), name="history-backfill")
        return True

    async def run(self) -> None:
        self._running = True
        await self.ensure_feed()
        if self._backfill_task is None:
            self._backfill_task = asyncio.create_task(self.backfill_history(), name="history-backfill")
        try:
            while self._running:
                try:
                    await self.ensure_feed()
                    payload = await asyncio.to_thread(self.collect_once)
                    await self.broadcast(self.live_payload() | {"event": "tick"})
                    _ = payload
                    if self._needs_backfill(self._backfill_task):
                        self._backfill_task = asyncio.create_task(self.backfill_history(), name="history-backfill")
                except FeedNotReady as exc:
                    self.error = str(exc)
                    log.info("tick waiting: %s", exc)
                    await self.broadcast({"event": "error", "status": self.status()})
                except Exception as exc:  # noqa: BLE001
                    self.error = str(exc)
                    log.exception("board tick failed")
                    await self.broadcast({"event": "error", "status": self.status()})
                try:
                    await asyncio.sleep(self.interval)
                except asyncio.CancelledError:
                    self._running = False
                    raise
        finally:
            if self.tasty_feed is not None:
                self.tasty_feed.stop()
            await self._stop_task(self._backfill_task)
            await self._stop_task(self._tasty_task)

    def _needs_backfill(self, task) -> bool:
        """After the 4pm roll the feed streams a new expiry whose history we have not pulled yet."""
        feed = self.tasty_feed
        if feed is None or task is None or not task.done():
            return False
        expiry = getattr(feed, "expiry", None)
        return bool(expiry) and expiry != self._backfill_expiry

    def stop(self) -> None:
        self._running = False
        if self.tasty_feed is not None:
            self.tasty_feed.stop()


def normalize_symbol(symbol: str | None, fallback: str) -> str:
    """Canonical board key: futures roots as '/NQ', equities/indices upper-cased ('QQQ', '^NDX')."""
    text = (symbol or "").strip().upper()
    if not text:
        return fallback
    if is_futures_root(text):
        return "/" + product_code(text)
    return text


class HubRegistry:
    """One LiveHub per symbol so viewers can watch NQ, ES and YM side by side.

    Hubs are created on first request and keep running for the life of the
    process; each has its own tastytrade feed, backfill, tick loop and SSE
    subscribers, so switching the index in one browser tab never touches another.
    """

    def __init__(self, default_symbol: str, start_collector: bool = True, **hub_kwargs: Any) -> None:
        self.default_symbol = normalize_symbol(default_symbol, default_symbol)
        self.start_collector = start_collector
        self.hub_kwargs = hub_kwargs
        self.hubs: dict[str, LiveHub] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.port = 8000
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()  # sync endpoints run in worker threads
        self.get(self.default_symbol)

    def key(self, symbol: str | None) -> str:
        return normalize_symbol(symbol, self.default_symbol)

    def get(self, symbol: str | None = None) -> LiveHub:
        key = self.key(symbol)
        with self._lock:
            hub = self.hubs.get(key)
            if hub is None:
                hub = LiveHub(symbol=key, **self.hub_kwargs)
                hub.port = self.port
                self.hubs[key] = hub
                if self._started:
                    self._launch(key, hub)
        return hub

    @property
    def default(self) -> LiveHub:
        return self.hubs[self.default_symbol]

    def _launch(self, key: str, hub: LiveHub) -> None:
        loop = self._loop
        if not self.start_collector or loop is None or key in self.tasks:
            return

        def spawn() -> None:
            if key not in self.tasks:
                self.tasks[key] = loop.create_task(hub.run(), name=f"optionsignal-collector-{key.strip('/')}")
                log.info("board started for %s", key)

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            spawn()
        else:
            # Created from a threadpool request handler: hand the task to the server loop.
            loop.call_soon_threadsafe(spawn)

    def start(self) -> None:
        """Call from the running event loop (app lifespan)."""
        self._loop = asyncio.get_running_loop()
        self._started = True
        for key, hub in list(self.hubs.items()):
            self._launch(key, hub)

    async def stop(self) -> None:
        self._started = False
        for hub in self.hubs.values():
            hub.stop()
        for task in self.tasks.values():
            task.cancel()
        for task in self.tasks.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.tasks.clear()

    def overview(self) -> list[dict]:
        return [
            {
                "symbol": key,
                "tick": hub.latest is not None,
                "error": hub.error,
                "viewers": len(hub.subscribers),
                "last_tick_at": hub.last_tick_at.isoformat(timespec="seconds") if hub.last_tick_at else None,
            }
            for key, hub in self.hubs.items()
        ]
