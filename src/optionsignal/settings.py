from __future__ import annotations

import os
from pathlib import Path


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def listen_port(default: int = 8000) -> int:
    """Railway injects PORT. Local default remains 8000."""
    return env_int("PORT", default)


def public_url() -> str | None:
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if domain:
        host = domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        return f"https://{host}"
    static = os.environ.get("RAILWAY_STATIC_URL")
    if static:
        static = static.strip()
        if static.startswith("http://") or static.startswith("https://"):
            return static.rstrip("/")
        return f"https://{static.rstrip('/')}"
    return None


def db_path() -> Path:
    explicit = os.environ.get("OPTIONSIGNAL_DB")
    if explicit:
        return Path(explicit)
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR")
    if volume:
        return Path(volume) / "optionsignal.db"
    return Path("data/optionsignal.db")
