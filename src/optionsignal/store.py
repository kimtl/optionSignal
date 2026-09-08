from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import SignalReport

DEFAULT_DB = Path("data/optionsignal.db")


def _connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts ON snapshots(symbol, ts)"
    )
    return conn


def save_snapshot(report: SignalReport, path: Path | None = None) -> int:
    path = path or DEFAULT_DB
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(path) as conn:
        cursor = conn.execute(
            "INSERT INTO snapshots (ts, symbol, payload) VALUES (?, ?, ?)",
            (ts, report.symbol, payload),
        )
        conn.commit()
        return int(cursor.lastrowid)


def load_history(symbol: str, limit: int = 200, path: Path | None = None) -> list[dict]:
    path = path or DEFAULT_DB
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT ts, payload FROM snapshots
            WHERE symbol = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (symbol.upper().lstrip("^"), limit),
        ).fetchall()
    history = []
    for ts, payload in reversed(rows):
        item = json.loads(payload)
        item["stored_at"] = ts
        history.append(item)
    return history
