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

from .collector import DEFAULT_INTERVAL, LiveHub
from .fetch import DEFAULT_SYMBOL
from .settings import default_interval, default_symbol
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
) -> FastAPI:
    hub = LiveHub(
        symbol=symbol or default_symbol(),
        max_dte=max_dte,
        band=band,
        interval=interval if interval is not None else default_interval(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hub = hub
        task = None
        if start_collector:
            task = asyncio.create_task(hub.run(), name="optionsignal-collector")
        try:
            yield
        finally:
            hub.stop()
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="optionSignal", lifespan=lifespan)
    app.state.hub = hub

    def _hub() -> LiveHub:
        return app.state.hub

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return files("optionsignal").joinpath("static/index.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health():
        hub = _hub()
        status = hub.status()
        return {
            "ok": True,
            "tick": hub.latest is not None,
            "error": hub.error,
            "source": status["source"],
            "realtime": status["realtime"],
            "phase": status.get("phase"),
            "contracts": status.get("contracts"),
            "quoted": status.get("quoted"),
        }

    @app.get("/api/live")
    def api_live():
        """Board payload. Always 200 so the UI can show a connecting/error state."""
        return _hub().live_payload()

    @app.get("/api/signal")
    def api_signal():
        """Latest shared tick. Does not fetch Yahoo per viewer."""
        hub = _hub()
        if hub.latest is None:
            raise HTTPException(
                status_code=503,
                detail=hub.error or "아직 첫 분봉을 찍지 않았습니다.",
            )
        return hub.latest

    @app.get("/api/minutes")
    def api_minutes(
        limit: int = Query(default=BAR_HISTORY_LIMIT, ge=10, le=RAW_HISTORY_LIMIT),
        tf: int = Query(default=1, ge=1, le=60),
    ):
        hub = _hub()
        raw = load_history(hub.symbol.lstrip("/"), limit=RAW_HISTORY_LIMIT)
        bars = aggregate_bars(raw, minutes=tf)
        return {"symbol": hub.symbol, "tf": tf, "points": [compact_point(item) for item in bars[-limit:]]}

    @app.get("/api/history")
    def api_history(limit: int = Query(default=BAR_HISTORY_LIMIT, ge=1, le=RAW_HISTORY_LIMIT)):
        hub = _hub()
        return JSONResponse(load_history(hub.symbol.lstrip("/"), limit=limit))

    @app.get("/api/status")
    def api_status():
        return _hub().status()

    @app.post("/api/settings")
    async def api_settings(
        symbol: str | None = Query(default=None),
        max_dte: int | None = Query(default=None, ge=0, le=7),
        interval: int | None = Query(default=None, ge=1, le=300),
    ):
        hub = _hub()
        if symbol:
            hub.symbol = symbol.upper()
        if max_dte is not None:
            hub.max_dte = max_dte
        if interval is not None:
            hub.interval = interval
        try:
            await asyncio.to_thread(hub.collect_once)
            await hub.broadcast(hub.live_payload() | {"event": "tick"})
        except Exception as exc:  # noqa: BLE001
            hub.error = str(exc)
            log.exception("settings tick failed")
            raise _http_from_exc(exc) from exc
        return hub.live_payload()

    @app.post("/api/tick")
    async def api_tick():
        hub = _hub()
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
    async def api_stream(request: Request):
        hub = _hub()
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
