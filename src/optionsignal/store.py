from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import SignalReport

DEFAULT_DB = Path("data/optionsignal.db")


def _connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
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
        conn.commit()
        return int(cursor.lastrowid)


def _rows_to_history(rows: list[tuple[str, str]]) -> list[dict]:
    history = []
    for ts, payload in rows:
        item = json.loads(payload)
        item["stored_at"] = ts
        history.append(item)
    return history


def load_history(symbol: str, limit: int = 240, path: Path | None = None) -> list[dict]:
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
    }
