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
    conn = sqlite3.connect(path, timeout=10)
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS option_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            key TEXT NOT NULL,
            expiry TEXT,
            mid REAL,
            bid REAL,
            ask REAL,
            last REAL,
            volume INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_option_ticks_symbol_key_ts ON option_ticks(symbol, key, ts)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL,
            key TEXT NOT NULL,
            ts TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume INTEGER,
            PRIMARY KEY (symbol, key, ts)
        )
        """
    )
    return conn


FUTURES_KEY = "FUT"  # key under which the underlying future's backfilled candles are stored


def option_key(strike: float, right: str) -> str:
    """'24700C' / '24700P' / '480.5P' — stable id for one contract within the 0DTE chain."""
    text = f"{float(strike):g}"
    return f"{text}{'C' if str(right).lower().startswith('c') else 'P'}"


def save_option_ticks(symbol: str, quotes, path: Path | None = None, now: datetime | None = None) -> int:
    """Store one price row per contract so any option can be charted after the fact."""
    now = now or datetime.now(timezone.utc)
    ts = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    symbol = symbol.upper().lstrip("^").lstrip("/")
    rows = []
    for q in quotes:
        mid = getattr(q, "mid", None)
        if mid is None or mid <= 0:
            continue
        rows.append((
            ts, symbol, option_key(q.strike, q.right), q.expiry.isoformat(),
            float(mid), getattr(q, "bid", None), getattr(q, "ask", None), getattr(q, "last", None),
            int(getattr(q, "volume", 0) or 0),
        ))
    if not rows:
        return 0
    with _connect(path) as conn:
        conn.executemany(
            "INSERT INTO option_ticks (ts, symbol, key, expiry, mid, bid, ask, last, volume) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        cutoff = (now - timedelta(hours=CHART_HOURS + 1)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM option_ticks WHERE ts < ?", (cutoff,))
        conn.commit()
    return len(rows)


def load_option_series(
    symbol: str,
    key: str,
    limit: int = RAW_HISTORY_LIMIT,
    path: Path | None = None,
    expiry: str | None = None,
) -> list[dict]:
    """Price rows for one contract key; pass `expiry` so a rolled-over chain does not mix contracts."""
    symbol = symbol.upper().lstrip("^").lstrip("/")
    where = "symbol = ? AND key = ?"
    params: list = [symbol, key]
    if expiry:
        where += " AND (expiry = ? OR expiry IS NULL)"
        params.append(expiry)
    params.append(limit)
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT ts, mid, bid, ask, last, volume FROM option_ticks WHERE {where} ORDER BY ts DESC LIMIT ?",
            params,
        ).fetchall()
    out = []
    for ts, mid, bid, ask, last, volume in reversed(rows):
        out.append({"asof": ts, "price": mid, "bid": bid, "ask": ask, "last": last, "volume": volume or 0})
    return out


def price_bars(points: list[dict], minutes: int = 1, value: str = "price") -> list[dict]:
    """Generic OHLC buckets for a plain price series (option mids)."""
    minutes = max(1, int(minutes))
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for point in points:
        ts = _parse_ts(point.get("asof") or point.get("stored_at"))
        px = point.get(value)
        if px is None:
            px = point.get("close")
        if ts is None or px is None:
            continue
        px = float(px)
        high = float(point["high"]) if point.get("high") is not None else px
        low = float(point["low"]) if point.get("low") is not None else px
        open_ = float(point["open"]) if point.get("open") is not None else px
        key_dt = floor_bar_time(ts, minutes)
        key = key_dt.isoformat()
        vol = int(point.get("volume") or 0)
        bar = buckets.get(key)
        if bar is None:
            buckets[key] = {
                "asof": key_dt.isoformat(timespec="seconds"),
                "open": open_, "high": high, "low": low, "close": px, "volume": vol,
            }
            order.append(key)
            continue
        bar["high"] = max(bar["high"], high)
        bar["low"] = min(bar["low"], low)
        bar["close"] = px
        bar["volume"] = max(bar["volume"], vol)
    return [buckets[k] for k in order]


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
        "nq_open": item.get("nq_open"),
        "nq_high": item.get("nq_high"),
        "nq_low": item.get("nq_low"),
        "nq_close": item.get("nq_close"),
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


def _futures_price(point: dict) -> float | None:
    for key in ("nq_close", "futures_price", "spot"):
        value = point.get(key)
        if value is not None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number == number and number > 0:
                return number
    return None


def aggregate_bars(points: list[dict], minutes: int = 1) -> list[dict]:
    """Bucket ticks (or smaller bars) into 1m/5m bars.

    Each bar carries NQ OHLC plus the CPPI OHLC when the point has one. Points with
    only a futures price (backfilled history) still produce a bar.
    """
    minutes = max(1, int(minutes))
    buckets: dict[str, dict] = {}
    order: list[str] = []
    points = sorted(points, key=lambda p: (_parse_ts(p.get("asof") or p.get("stored_at")) or datetime.min.replace(tzinfo=timezone.utc)))
    for point in points:
        ts = _parse_ts(point.get("asof") or point.get("stored_at"))
        if ts is None:
            continue
        close = point.get("close")
        if close is None:
            close = point.get("headline_cppi")
        nq_close = _futures_price(point)
        if close is None and nq_close is None:
            continue
        has_cppi = close is not None
        if has_cppi:
            close = float(close)
            high = float(point.get("high") if point.get("high") is not None else close)
            low = float(point.get("low") if point.get("low") is not None else close)
            open_ = float(point.get("open") if point.get("open") is not None else close)
        else:
            high = low = open_ = None
        nq_high = float(point["nq_high"]) if point.get("nq_high") is not None else nq_close
        nq_low = float(point["nq_low"]) if point.get("nq_low") is not None else nq_close
        nq_open = float(point["nq_open"]) if point.get("nq_open") is not None else nq_close
        key_dt = floor_bar_time(ts, minutes)
        # Backfilled candles are stored in UTC while live ticks carry New York offsets;
        # bucket on the instant so the same minute never yields two bars.
        key = key_dt.astimezone(timezone.utc).isoformat()
        call_prem = float(point.get("headline_call_premium") or 0) if has_cppi else None
        put_prem = float(point.get("headline_put_premium") or 0) if has_cppi else None
        bar = buckets.get(key)
        if bar is None:
            bar = {
                "asof": key_dt.isoformat(timespec="seconds"),
                "stored_at": point.get("stored_at"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "headline_cppi": close,
                "headline_call_premium": call_prem,
                "headline_put_premium": put_prem,
                "nq_open": nq_open,
                "nq_high": nq_high,
                "nq_low": nq_low,
                "nq_close": nq_close,
                "futures_price": nq_close,
                "_first_call": call_prem,
                "_first_put": put_prem,
            }
            buckets[key] = bar
            order.append(key)
            continue
        if has_cppi:
            if bar["close"] is None:
                bar["open"], bar["high"], bar["low"] = open_, high, low
                bar["_first_call"], bar["_first_put"] = call_prem, put_prem
            else:
                bar["high"] = max(bar["high"], high)
                bar["low"] = min(bar["low"], low)
            bar["close"] = close
            bar["headline_cppi"] = close
            bar["headline_call_premium"] = call_prem
            bar["headline_put_premium"] = put_prem
            bar["stored_at"] = point.get("stored_at") or bar.get("stored_at")
        if nq_close is not None:
            if bar["nq_open"] is None:
                bar["nq_open"] = nq_open
            bar["nq_high"] = nq_high if bar["nq_high"] is None else max(bar["nq_high"], nq_high)
            bar["nq_low"] = nq_low if bar["nq_low"] is None else min(bar["nq_low"], nq_low)
            bar["nq_close"] = nq_close
            bar["futures_price"] = nq_close
    out: list[dict] = []
    prev_close = None
    for key in order:
        bar = buckets[key]
        if bar["close"] is not None:
            bar["call_premium_delta_1m"] = bar["headline_call_premium"] - bar["_first_call"]
            bar["put_premium_delta_1m"] = bar["headline_put_premium"] - bar["_first_put"]
            bar["cppi_delta_1m"] = None if prev_close is None else bar["close"] - prev_close
            prev_close = bar["close"]
        else:
            bar["call_premium_delta_1m"] = None
            bar["put_premium_delta_1m"] = None
            bar["cppi_delta_1m"] = None
        bar.pop("_first_call", None)
        bar.pop("_first_put", None)
        out.append(bar)
    return out


def save_bars(symbol: str, key: str, bars: list[dict], path: Path | None = None) -> int:
    """Store backfilled 1m candles (from dxFeed/Yahoo) for the future or a contract."""
    symbol = symbol.upper().lstrip("^").lstrip("/")
    rows = []
    for bar in bars:
        ts = _parse_ts(bar.get("asof") or bar.get("ts"))
        close = bar.get("close")
        if ts is None or close is None:
            continue
        ts_utc = ts.astimezone(timezone.utc).isoformat(timespec="seconds")
        rows.append((
            symbol, key, ts_utc,
            float(bar.get("open") if bar.get("open") is not None else close),
            float(bar.get("high") if bar.get("high") is not None else close),
            float(bar.get("low") if bar.get("low") is not None else close),
            float(close), int(bar.get("volume") or 0),
        ))
    if not rows:
        return 0
    with _connect(path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bars (symbol, key, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=CHART_HOURS + 1)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM bars WHERE ts < ?", (cutoff,))
        conn.commit()
    return len(rows)


def load_bars(symbol: str, key: str, limit: int = BAR_HISTORY_LIMIT, path: Path | None = None) -> list[dict]:
    symbol = symbol.upper().lstrip("^").lstrip("/")
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ? AND key = ? ORDER BY ts DESC LIMIT ?",
            (symbol, key, limit),
        ).fetchall()
    return [
        {"asof": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}
        for ts, o, h, l, c, v in reversed(rows)
    ]


def futures_history_points(symbol: str, path: Path | None = None) -> list[dict]:
    """Backfilled NQ candles shaped like tick points so aggregate_bars can merge them."""
    return [
        {
            "asof": bar["asof"],
            "nq_open": bar["open"],
            "nq_high": bar["high"],
            "nq_low": bar["low"],
            "nq_close": bar["close"],
            "futures_price": bar["close"],
            "backfill": True,
        }
        for bar in load_bars(symbol, FUTURES_KEY, path=path)
    ]


def backfill_option_bars(
    symbol: str, key: str, bars: list[dict], expiry: str | None = None, path: Path | None = None
) -> int:
    """Insert historical candles into option_ticks for times before the first live tick.

    Backfilled rows have bid/ask NULL so they can be replaced by a later backfill.
    """
    symbol = symbol.upper().lstrip("^").lstrip("/")
    scope = "symbol = ? AND key = ?" + (" AND expiry = ?" if expiry else "")
    scope_params: tuple = (symbol, key, expiry) if expiry else (symbol, key)
    with _connect(path) as conn:
        first_live = conn.execute(
            f"SELECT MIN(ts) FROM option_ticks WHERE {scope} AND bid IS NOT NULL", scope_params
        ).fetchone()[0]
        conn.execute(f"DELETE FROM option_ticks WHERE {scope} AND bid IS NULL", scope_params)
        rows = []
        for bar in bars:
            ts = _parse_ts(bar.get("asof") or bar.get("ts"))
            close = bar.get("close")
            if ts is None or close is None or close <= 0:
                continue
            ts_utc = ts.astimezone(timezone.utc).isoformat(timespec="seconds")
            if first_live and ts_utc >= first_live:
                continue
            rows.append((ts_utc, symbol, key, expiry, float(close), None, None, float(close), int(bar.get("volume") or 0)))
        if rows:
            conn.executemany(
                "INSERT INTO option_ticks (ts, symbol, key, expiry, mid, bid, ask, last, volume) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        conn.commit()
    return len(rows)


def load_option_rows(
    symbol: str,
    keys: list[str],
    expiry: str | None = None,
    limit: int = RAW_HISTORY_LIMIT,
    live_only: bool = True,
    path: Path | None = None,
) -> dict[str, list[dict]]:
    """{key: [{asof, mid, volume}, ...]} for several contracts at once (oldest first).

    live_only skips backfilled candle rows (bid IS NULL), whose volume is per-minute rather
    than the day total the live quotes carry, so premium sums stay comparable.
    """
    symbol = symbol.upper().lstrip("^").lstrip("/")
    keys = [k for k in keys if k]
    if not keys:
        return {}
    where = f"symbol = ? AND key IN ({','.join('?' * len(keys))})"
    params: list = [symbol, *keys]
    if expiry:
        where += " AND (expiry = ? OR expiry IS NULL)"
        params.append(expiry)
    if live_only:
        where += " AND bid IS NOT NULL"
    params.append(limit * len(keys))
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT ts, key, mid, volume FROM option_ticks WHERE {where} ORDER BY ts DESC LIMIT ?",
            params,
        ).fetchall()
    out: dict[str, list[dict]] = {k: [] for k in keys}
    for ts, key, mid, volume in reversed(rows):
        out.setdefault(key, []).append({"asof": ts, "mid": mid, "volume": volume or 0})
    return out


def premium_ratio_points(rows_by_key: dict[str, list[dict]], calls: list[str], puts: list[str]) -> list[dict]:
    """Per tick: Σ(mid × volume) over the chosen calls ÷ the same over the puts × 100.

    Ticks are stored with one shared timestamp per collect, so rows are matched on `asof`.
    A tick needs at least one call and one put row and a positive put sum to produce a point.
    """
    calls = [k for k in calls if k in rows_by_key]
    puts = [k for k in puts if k in rows_by_key]
    if not calls or not puts:
        return []
    by_ts: dict[str, dict] = {}
    for side, keys in (("call", calls), ("put", puts)):
        for key in keys:
            for row in rows_by_key.get(key, []):
                mid = row.get("mid")
                if mid is None or mid <= 0:
                    continue
                bucket = by_ts.setdefault(row["asof"], {"call": 0.0, "put": 0.0, "call_n": 0, "put_n": 0})
                bucket[side] += float(mid) * float(row.get("volume") or 0)
                bucket[f"{side}_n"] += 1
    out = []
    for ts in sorted(by_ts):
        b = by_ts[ts]
        if not b["call_n"] or not b["put_n"] or b["put"] <= 0:
            continue
        out.append({
            "asof": ts,
            "ratio": b["call"] / b["put"] * 100.0,
            "call_premium": b["call"],
            "put_premium": b["put"],
        })
    return out
