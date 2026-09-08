from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any, Callable

from .fetch import DEFAULT_SYMBOL, fetch_chain
from .settings import default_interval, public_url, tasty_configured
from .signal import DEFAULT_BAND, DEFAULT_HEADLINE_DTE, build_report, with_deltas, yahoo_session_status
from .store import RAW_HISTORY_LIMIT, BAR_HISTORY_LIMIT, aggregate_bars, compact_point, load_history, save_snapshot
from .tasty import FeedConfigError, FeedNotReady, TastyFeed, is_futures_root

log = logging.getLogger("optionsignal")

FetchFn = Callable[..., Any]
DEFAULT_INTERVAL = 60


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
        self.error: str | None = None
        self.last_tick_at: datetime | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self._running = False
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
        return {
            "symbol": self.symbol,
            "max_dte": self.max_dte,
            "otm_points": self.otm_points,
            "band": self.band,
            "interval": self.interval,
            "viewers": len(self.subscribers),
            "last_tick_at": self.last_tick_at.isoformat(timespec="seconds") if self.last_tick_at else None,
            "error": self.error or feed_error,
            "session": session,
            "source": source,
            "realtime": realtime,
            "ready": ready,
            "phase": getattr(feed, "phase", None) if feed else None,
            "contracts": len(getattr(feed, "contracts", []) or []) if feed else 0,
            "quoted": int(getattr(feed, "quoted_count", 0) or 0) if feed else 0,
            "share_hint": url or lan_ip(),
            "public_url": url,
            "port": self.port,
        }

    def live_payload(self) -> dict:
        raw = load_history(self.symbol.lstrip("/"), limit=RAW_HISTORY_LIMIT)
        bars = aggregate_bars(raw, minutes=1)
        points = [compact_point(item) for item in bars[-BAR_HISTORY_LIMIT:]]
        return {
            "tick": self.latest,
            "points": points,
            "status": self.status(),
        }

    def collect_once(self) -> dict:
        history = load_history(self.symbol.lstrip("/"), limit=240)
        chain = self._load_chain()
        report = build_report(
            chain, max_dte=self.max_dte, moneyness_band=self.band, otm_points=self.otm_points
        )
        payload = with_deltas(report.to_dict(), history)
        save_snapshot(payload)
        self.latest = payload
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

    async def run(self) -> None:
        self._running = True
        tasty_task = None
        if tasty_configured() and self.tasty_feed is None:
            self.tasty_feed = TastyFeed(symbol=self.symbol, max_dte=self.max_dte, band=self.band)
        if self.tasty_feed is not None:
            tasty_task = asyncio.create_task(self.tasty_feed.run(), name="tasty-dxlink")
        try:
            while self._running:
                try:
                    payload = await asyncio.to_thread(self.collect_once)
                    await self.broadcast(self.live_payload() | {"event": "tick"})
                    _ = payload
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
            if tasty_task:
                tasty_task.cancel()
                try:
                    await tasty_task
                except asyncio.CancelledError:
                    pass

    def stop(self) -> None:
        self._running = False
        if self.tasty_feed is not None:
            self.tasty_feed.stop()
