from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal

Right = Literal["call", "put"]


@dataclass(frozen=True)
class OptionQuote:
    expiry: date
    right: Right
    strike: float
    bid: float | None
    ask: float | None
    last: float | None
    mid: float | None
    volume: int
    open_interest: int
    iv: float | None
    percent_change: float | None


@dataclass
class OptionChain:
    symbol: str
    spot: float
    futures_symbol: str
    futures_price: float | None
    asof: datetime
    quotes: list[OptionQuote]
    multiplier: int = 100
    source: str = "yahoo"


@dataclass
class ExpirySlice:
    expiry: str
    dte: int
    call_premium: float
    put_premium: float
    call_volume: int
    put_volume: int
    cppi: float | None
    otm_call_iv: float | None
    otm_put_iv: float | None
    risk_reversal: float | None
    call_surge_pct: float | None
    put_surge_pct: float | None
    wing_call_mid: float | None
    wing_put_mid: float | None
    wing_pct: float


@dataclass
class SignalReport:
    symbol: str
    spot: float
    futures_symbol: str
    futures_price: float | None
    asof: str
    source: str
    max_dte: int
    moneyness_band: float
    headline_cppi: float | None
    headline_call_premium: float
    headline_put_premium: float
    headline_call_notional: float
    headline_put_notional: float
    headline_call_volume: int
    headline_put_volume: int
    premium_ratio: float | None
    risk_reversal: float | None
    otm_call_iv: float | None
    otm_put_iv: float | None
    session_call_surge_pct: float | None
    session_put_surge_pct: float | None
    session_surge_gap: float | None
    bias: str
    score: int
    summary_ko: str
    summary_en: str
    slices: list[ExpirySlice] = field(default_factory=list)
    otm_points: float | None = None
    headline_call_strikes: list[float] | None = None
    headline_put_strikes: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload
