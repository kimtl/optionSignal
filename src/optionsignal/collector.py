from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timezone
from typing import Any, Callable

from .fetch import DEFAULT_SYMBOL, fetch_chain
from .signal import DEFAULT_BAND, DEFAULT_HEADLINE_DTE, build_report, with_deltas, yahoo_session_status
from .store import compact_point, load_history, save_snapshot

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
    """One shared minute poller. Every browser watches the same tick."""

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        max_dte: int = DEFAULT_HEADLINE_DTE,
        band: float = DEFAULT_BAND,
        interval: int = DEFAULT_INTERVAL,
        fetch_fn: FetchFn | None = None,
    ) -> None:
        self.symbol = symbol
        self.max_dte = max_dte
        self.band = band
        self.interval = max(15, int(interval))
        self.fetch_fn = fetch_fn or fetch_chain
        self.latest: dict | None = None
        self.error: str | None = None
        self.last_tick_at: datetime | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self._running = False
        self.port = 8000

    def status(self) -> dict:
        return {
            "symbol": self.symbol,
            "max_dte": self.max_dte,
            "interval": self.interval,
            "viewers": len(self.subscribers),
            "last_tick_at": self.last_tick_at.isoformat(timespec="seconds") if self.last_tick_at else None,
            "error": self.error,
            "session": yahoo_session_status(),
            "share_hint": lan_ip(),
            "port": self.port,
        }

    def live_payload(self) -> dict:
        points = [compact_point(item) for item in load_history(self.symbol, limit=240)]
        return {
            "tick": self.latest,
            "points": points,
            "status": self.status(),
        }

    def collect_once(self) -> dict:
        history = load_history(self.symbol, limit=240)
        chain = self.fetch_fn(
            symbol=self.symbol,
            max_dte=self.max_dte,
            expiry_limit=max(2, self.max_dte + 1),
        )
        report = build_report(chain, max_dte=self.max_dte, moneyness_band=self.band)
        payload = with_deltas(report.to_dict(), history)
        save_snapshot(payload)
        self.latest = payload
        self.error = None
        self.last_tick_at = datetime.now(timezone.utc)
        return payload

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
        while self._running:
            try:
                payload = await asyncio.to_thread(self.collect_once)
                await self.broadcast(self.live_payload() | {"event": "tick"})
                _ = payload
            except Exception as exc:  # noqa: BLE001 — keep the loop alive for the shared board
                self.error = str(exc)
                await self.broadcast({"event": "error", "status": self.status()})
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                self._running = False
                raise

    def stop(self) -> None:
        self._running = False
