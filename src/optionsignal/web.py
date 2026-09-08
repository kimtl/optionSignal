from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .fetch import DEFAULT_SYMBOL, fetch_chain
from .signal import DEFAULT_HEADLINE_DTE, build_report
from .store import load_history, save_snapshot

STATIC_DIR = Path(__file__).resolve().parent / "static"


@lru_cache(maxsize=4)
def _cached_report(symbol: str, max_dte: int, band: float) -> dict:
    chain = fetch_chain(symbol=symbol, max_dte=max(max_dte, 45))
    return build_report(chain, max_dte=max_dte, moneyness_band=band).to_dict()


def create_app(symbol: str = DEFAULT_SYMBOL, max_dte: int = DEFAULT_HEADLINE_DTE) -> FastAPI:
    app = FastAPI(title="optionSignal")
    app.state.default_symbol = symbol
    app.state.default_max_dte = max_dte

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return files("optionsignal").joinpath("static/index.html").read_text(encoding="utf-8")

    @app.get("/api/signal")
    def api_signal(
        symbol: str | None = Query(default=None),
        max_dte: int | None = Query(default=None),
        band: float = Query(default=0.08, ge=0.02, le=0.25),
        refresh: bool = Query(default=False),
    ):
        use_symbol = (symbol or app.state.default_symbol).upper()
        use_dte = app.state.default_max_dte if max_dte is None else max_dte
        if refresh:
            _cached_report.cache_clear()
        try:
            return _cached_report(use_symbol, use_dte, band)
        except Exception as exc:  # noqa: BLE001 — surface data/vendor errors to the UI
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/snapshot")
    def api_snapshot(
        symbol: str | None = Query(default=None),
        max_dte: int | None = Query(default=None),
        band: float = Query(default=0.08),
    ):
        use_symbol = (symbol or app.state.default_symbol).upper()
        use_dte = app.state.default_max_dte if max_dte is None else max_dte
        _cached_report.cache_clear()
        try:
            chain = fetch_chain(symbol=use_symbol, max_dte=max(use_dte, 45))
            report = build_report(chain, max_dte=use_dte, moneyness_band=band)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        row_id = save_snapshot(report)
        payload = report.to_dict()
        payload["snapshot_id"] = row_id
        return payload

    @app.get("/api/history")
    def api_history(
        symbol: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ):
        use_symbol = (symbol or app.state.default_symbol).upper()
        return JSONResponse(load_history(use_symbol, limit=limit))

    return app


app = create_app()
