from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import SignalReport
from .settings import db_path

DEFAULT_DB = Path("data/optionsignal.db")
CHART_HOURS = 12
RAW_HISTORY_LIMIT = CHART_HOURS * 720 + 240  # 12h of 5s ticks, plus a little
BAR_HISTORY_LIMIT = CHART_HOURS * 60  # 1-minute bars


def _resolve_db(path: Path | None = None) -> Path:
    if path is not None:
        return path
    env_path = db_path()
    if env_path != Path("data/optionsignal.db"):
        return env_path
    return DEFAULT_DB


def _connect(path: Path | None = None) -> sqlite3.Connection:
    path = _resolve_db(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ticks'"
    ).fetchone()
    if existing and existing[0] and "UNIQUE" in existing[0].upper():
        conn.execute("DROP TABLE ticks")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            minute TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts ON snapshots(symbol, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, ts)"
    )
    return conn


def minute_key(ts: datetime | None = None) -> str:
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def save_snapshot(report: SignalReport | dict, path: Path | None = None) -> int:
    payload_obj = report.to_dict() if isinstance(report, SignalReport) else dict(report)
    payload = json.dumps(payload_obj, ensure_ascii=False)
    now = datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    symbol = str(payload_obj.get("symbol", "QQQ")).upper().lstrip("^")
    with _connect(path) as conn:
        cursor = conn.execute(
            "INSERT INTO snapshots (ts, symbol, payload) VALUES (?, ?, ?)",
            (ts, symbol, payload),
        )
        conn.execute(
            "INSERT INTO ticks (ts, minute, symbol, payload) VALUES (?, ?, ?, ?)",
            (ts, minute_key(now), symbol, payload),
        )
        cutoff = (now - timedelta(hours=CHART_HOURS + 1)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        conn.commit()
        return int(cursor.lastrowid)


def _rows_to_history(rows: list[tuple[str, str]]) -> list[dict]:
    history = []
    for ts, payload in rows:
        item = json.loads(payload)
        item["stored_at"] = ts
        history.append(item)
    return history


def load_history(symbol: str, limit: int = RAW_HISTORY_LIMIT, path: Path | None = None) -> list[dict]:
    symbol = symbol.upper().lstrip("^")
    with _connect(path) as conn:
        tick_count = conn.execute("SELECT COUNT(*) FROM ticks WHERE symbol = ?", (symbol,)).fetchone()[0]
        if tick_count:
            rows = conn.execute(
                """
                SELECT ts, payload FROM ticks
                WHERE symbol = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
            return _rows_to_history(list(reversed(rows)))
        rows = conn.execute(
            """
            SELECT ts, payload FROM snapshots
            WHERE symbol = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()
    return _rows_to_history(list(reversed(rows)))


def compact_point(item: dict) -> dict:
    return {
        "stored_at": item.get("stored_at"),
        "asof": item.get("asof"),
        "headline_cppi": item.get("headline_cppi"),
        "headline_call_premium": item.get("headline_call_premium"),
        "headline_put_premium": item.get("headline_put_premium"),
        "risk_reversal": item.get("risk_reversal"),
        "session_surge_gap": item.get("session_surge_gap"),
        "spot": item.get("spot"),
        "futures_price": item.get("futures_price"),
        "cppi_delta_1m": item.get("cppi_delta_1m"),
        "cppi_delta_5m": item.get("cppi_delta_5m"),
        "call_premium_delta_1m": item.get("call_premium_delta_1m"),
        "put_premium_delta_1m": item.get("put_premium_delta_1m"),
        "flow_1m": item.get("flow_1m"),
        "bias": item.get("bias"),
        "score": item.get("score"),
        "open": item.get("open"),
        "high": item.get("high"),
        "low": item.get("low"),
        "close": item.get("close"),
    }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def floor_bar_time(ts: datetime, minutes: int) -> datetime:
    minutes = max(1, int(minutes))
    ts = ts.replace(second=0, microsecond=0)
    extra = ts.minute % minutes
    if extra:
        ts = ts - timedelta(minutes=extra)
    return ts


def aggregate_bars(points: list[dict], minutes: int = 1) -> list[dict]:
    """Bucket ticks (or smaller bars) into 1m/5m CPPI candles."""
    minutes = max(1, int(minutes))
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for point in points:
        ts = _parse_ts(point.get("asof") or point.get("stored_at"))
        close = point.get("close")
        if close is None:
            close = point.get("headline_cppi")
        if ts is None or close is None:
            continue
        close = float(close)
        high = float(point.get("high") if point.get("high") is not None else close)
        low = float(point.get("low") if point.get("low") is not None else close)
        open_ = float(point.get("open") if point.get("open") is not None else close)
        key_dt = floor_bar_time(ts, minutes)
        key = key_dt.isoformat()
        call_prem = float(point.get("headline_call_premium") or 0)
        put_prem = float(point.get("headline_put_premium") or 0)
        if key not in buckets:
            buckets[key] = {
                "asof": key_dt.isoformat(timespec="seconds"),
                "stored_at": point.get("stored_at"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "headline_cppi": close,
                "headline_call_premium": call_prem,
                "headline_put_premium": put_prem,
                "_first_call": call_prem,
                "_first_put": put_prem,
            }
            order.append(key)
            continue
        bar = buckets[key]
        bar["high"] = max(bar["high"], high)
        bar["low"] = min(bar["low"], low)
        bar["close"] = close
        bar["headline_cppi"] = close
        bar["headline_call_premium"] = call_prem
        bar["headline_put_premium"] = put_prem
        bar["stored_at"] = point.get("stored_at") or bar.get("stored_at")
    out: list[dict] = []
    prev_close = None
    for key in order:
        bar = buckets[key]
        bar["call_premium_delta_1m"] = bar["headline_call_premium"] - bar["_first_call"]
        bar["put_premium_delta_1m"] = bar["headline_put_premium"] - bar["_first_put"]
        bar["cppi_delta_1m"] = None if prev_close is None else bar["close"] - prev_close
        prev_close = bar["close"]
        bar.pop("_first_call", None)
        bar.pop("_first_put", None)
        out.append(bar)
    return out
