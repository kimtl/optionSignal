from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .collector import DEFAULT_INTERVAL, HubRegistry, LiveHub
from .fetch import DEFAULT_SYMBOL
from .settings import default_interval, default_otm_points, default_symbol
from .signal import DEFAULT_HEADLINE_DTE
from .store import BAR_HISTORY_LIMIT, RAW_HISTORY_LIMIT, aggregate_bars, compact_point, load_history

log = logging.getLogger("optionsignal")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _http_from_exc(exc: Exception) -> HTTPException:
    status = int(getattr(exc, "status_code", 502) or 502)
    return HTTPException(status_code=status, detail=str(exc))


def create_app(
    symbol: str | None = None,
    max_dte: int = DEFAULT_HEADLINE_DTE,
    interval: int | None = None,
    start_collector: bool = True,
    band: float = 0.08,
    otm_points: float | None = None,
) -> FastAPI:
    hubs = HubRegistry(
        symbol or default_symbol(),
        start_collector=start_collector,
        max_dte=max_dte,
        band=band,
        interval=interval if interval is not None else default_interval(),
        otm_points=otm_points if otm_points is not None else default_otm_points(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hubs = hubs
        app.state.hub = hubs.default
        hubs.start()
        try:
            yield
        finally:
            await hubs.stop()

    app = FastAPI(title="optionSignal", lifespan=lifespan)
    app.state.hubs = hubs
    app.state.hub = hubs.default  # the server's default board (CLI/tests); viewers pick their own via ?symbol=

    def _hub(symbol: str | None = None) -> LiveHub:
        """Board for one symbol. Each browser tab passes its own ?symbol=, so tabs never share state."""
        return app.state.hubs.get(symbol)

    SymbolQ = Query(default=None, description="board symbol, e.g. /NQ, /ES, /YM, QQQ (default: server default)")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return files("optionsignal").joinpath("static/index.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health(symbol: str | None = SymbolQ):
        hub = _hub(symbol)
        status = hub.status()
        return {
            "ok": True,
            "symbol": hub.symbol,
            "tick": hub.latest is not None,
            "error": hub.error,
            "source": status["source"],
            "realtime": status["realtime"],
            "phase": status.get("phase"),
            "contracts": status.get("contracts"),
            "quoted": status.get("quoted"),
            "boards": app.state.hubs.overview(),
        }

    @app.get("/api/boards")
    def api_boards():
        """Every board this server is running (one per symbol viewers asked for)."""
        return {"default": app.state.hubs.default_symbol, "boards": app.state.hubs.overview()}

    @app.get("/api/live")
    def api_live(symbol: str | None = SymbolQ):
        """Board payload. Always 200 so the UI can show a connecting/error state."""
        return _hub(symbol).live_payload()

    @app.get("/api/signal")
    def api_signal(symbol: str | None = SymbolQ):
        """Latest shared tick. Does not fetch Yahoo per viewer."""
        hub = _hub(symbol)
        if hub.latest is None:
            raise HTTPException(
                status_code=503,
                detail=hub.error or "아직 첫 분봉을 찍지 않았습니다.",
            )
        return hub.latest

    @app.get("/api/premium_ratio")
    def api_premium_ratio(
        calls: str = Query(default=""),
        puts: str = Query(default=""),
        tf: int = Query(default=1, ge=1, le=60),
        limit: int = Query(default=BAR_HISTORY_LIMIT, ge=10, le=RAW_HISTORY_LIMIT),
        symbol: str | None = SymbolQ,
    ):
        split = lambda text: [k.strip().upper() for k in text.split(",") if k.strip()]  # noqa: E731
        return JSONResponse(_hub(symbol).premium_ratio_series(split(calls), split(puts), tf=tf, limit=limit))

    @app.get("/api/chain")
    def api_chain(symbol: str | None = SymbolQ):
        return _hub(symbol).chain_payload()

    @app.get("/api/option_series")
    def api_option_series(
        keys: str = Query(default="", description="comma-separated, e.g. 24700C,24650P"),
        tf: int = Query(default=1, ge=1, le=60),
        limit: int = Query(default=BAR_HISTORY_LIMIT, ge=10, le=RAW_HISTORY_LIMIT),
        symbol: str | None = SymbolQ,
    ):
        wanted = [k.strip().upper() for k in keys.split(",") if k.strip()]
        return _hub(symbol).option_series(wanted, tf=tf, limit=limit)

    @app.get("/api/minutes")
    def api_minutes(
        limit: int = Query(default=BAR_HISTORY_LIMIT, ge=10, le=RAW_HISTORY_LIMIT),
        tf: int = Query(default=1, ge=1, le=60),
        symbol: str | None = SymbolQ,
    ):
        hub = _hub(symbol)
        bars = aggregate_bars(hub.history_points(), minutes=tf)
        return {"symbol": hub.symbol, "tf": tf, "points": [compact_point(item) for item in bars[-limit:]]}

    @app.get("/api/history")
    def api_history(
        limit: int = Query(default=BAR_HISTORY_LIMIT, ge=1, le=RAW_HISTORY_LIMIT),
        symbol: str | None = SymbolQ,
    ):
        hub = _hub(symbol)
        return JSONResponse(load_history(hub.symbol.lstrip("/"), limit=limit))

    @app.get("/api/status")
    def api_status(symbol: str | None = SymbolQ):
        return _hub(symbol).status()

    @app.post("/api/settings")
    async def api_settings(
        symbol: str | None = SymbolQ,
        max_dte: int | None = Query(default=None, ge=0, le=7),
        interval: int | None = Query(default=None, ge=1, le=300),
        otm_points: float | None = Query(default=None, ge=0, le=5000),
    ):
        """Settings of one board. `symbol` selects (and starts, if needed) that board;
        it no longer re-points a shared board, so other viewers are unaffected."""
        hub = _hub(symbol)
        if max_dte is not None:
            hub.max_dte = max_dte
        if otm_points is not None:
            # 0 means "back to the percent band".
            hub.otm_points = otm_points if otm_points > 0 else None
        if interval is not None:
            hub.interval = interval
        if await hub.ensure_feed():
            # New symbol: the feed is loading its chain; the loop ticks as soon as quotes arrive.
            hub.error = f"{hub.symbol} 체인을 불러오는 중입니다."
            await hub.broadcast({"event": "error", "status": hub.status()})
            return hub.live_payload()
        try:
            await asyncio.to_thread(hub.collect_once)
            await hub.broadcast(hub.live_payload() | {"event": "tick"})
        except Exception as exc:  # noqa: BLE001
            hub.error = str(exc)
            log.exception("settings tick failed")
            raise _http_from_exc(exc) from exc
        return hub.live_payload()

    @app.post("/api/tick")
    async def api_tick(symbol: str | None = SymbolQ):
        hub = _hub(symbol)
        try:
            await asyncio.to_thread(hub.collect_once)
            payload = hub.live_payload() | {"event": "tick"}
            await hub.broadcast(payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            hub.error = str(exc)
            log.exception("POST /api/tick failed")
            raise _http_from_exc(exc) from exc

    @app.get("/api/stream")
    async def api_stream(request: Request, symbol: str | None = SymbolQ):
        hub = _hub(symbol)
        queue = hub.subscribe()

        async def events() -> AsyncIterator[str]:
            try:
                yield f"data: {json.dumps(hub.live_payload() | {'event': 'hello'}, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'event': 'ping', 'status': hub.status()}, ensure_ascii=False)}\n\n"
                        continue
                    yield f"data: {message}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app(start_collector=False)
