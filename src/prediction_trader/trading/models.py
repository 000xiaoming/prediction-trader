"""Venue-independent contracts. All financial arithmetic uses finite Decimals."""

import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")


class TradingError(ValueError):
    """Invalid market input or a rejected local paper action."""


class InstrumentKind(StrEnum):
    BINARY_EVENT = "binary_event"
    SPOT = "spot"
    PERPETUAL = "perpetual"


def identifier(value: str, label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value):
        raise TradingError(f"Invalid {label}")


def finite(value: Decimal, label: str, minimum: Decimal | None = None) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TradingError(f"{label} must be a finite Decimal")
    if minimum is not None and value < minimum:
        raise TradingError(f"{label} must be >= {minimum}")


def aware(value: datetime, label: str = "timestamp") -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TradingError(f"{label} must be timezone-aware")


def jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    instrument_id: str
    venue: str
    kind: InstrumentKind
    symbol: str
    base_currency: str
    quote_currency: str
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    maximum_leverage: Decimal = ONE
    maintenance_margin_rate: Decimal = Decimal("0.01")
    maximum_quantity: Decimal | None = None
    market_id: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    outcome: str | None = None
    title: str | None = None
    expires_at: datetime | None = None
    rules_source: str = "paper_defaults_refresh_before_public_quote"

    def __post_init__(self) -> None:
        for name in ("instrument_id", "venue", "symbol", "base_currency", "quote_currency"):
            identifier(getattr(self, name), name)
        if not isinstance(self.kind, InstrumentKind):
            raise TradingError("kind must be InstrumentKind")
        for name in ("price_tick", "quantity_step", "minimum_quantity"):
            finite(getattr(self, name), name)
            if getattr(self, name) <= ZERO:
                raise TradingError(f"{name} must be positive")
        finite(self.minimum_notional, "minimum_notional", ZERO)
        finite(self.maximum_leverage, "maximum_leverage", ONE)
        finite(self.maintenance_margin_rate, "maintenance_margin_rate", ZERO)
        if self.maintenance_margin_rate >= ONE / self.maximum_leverage:
            raise TradingError("maintenance margin must be below initial margin")
        if self.maximum_quantity is not None:
            finite(self.maximum_quantity, "maximum_quantity", self.minimum_quantity)
        if self.kind != InstrumentKind.PERPETUAL and self.maximum_leverage != ONE:
            raise TradingError("Only perpetuals support leverage")
        if self.expires_at is not None:
            aware(self.expires_at, "expires_at")
        if self.kind == InstrumentKind.BINARY_EVENT:
            if self.venue != "polymarket" or self.outcome not in {"YES", "NO"}:
                raise TradingError("Binary event requires Polymarket YES/NO")
            if not self.market_id or not self.condition_id or not self.token_id:
                raise TradingError("Binary event requires actual market, condition, and token IDs")
            if not self.market_id.isdigit() or not self.token_id.isdigit():
                raise TradingError("Market and token IDs must be numeric")
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", self.condition_id):
                raise TradingError("Invalid condition ID")
            if self.price_tick >= ONE:
                raise TradingError("Invalid binary price tick")


@dataclass(frozen=True, slots=True)
class Quote:
    instrument_id: str
    portfolio_id: str
    observed_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    mark_price: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_at: datetime | None = None
    source: str = "fixture"
    timestamp_basis: str = "received_at"

    def __post_init__(self) -> None:
        identifier(self.instrument_id, "instrument_id")
        identifier(self.portfolio_id, "portfolio_id")
        aware(self.observed_at, "observed_at")
        for name in ("bid", "ask", "bid_size", "ask_size"):
            finite(getattr(self, name), name, ZERO)
        if self.bid <= ZERO or self.ask <= ZERO or self.bid > self.ask:
            raise TradingError("Quote must have positive, uncrossed bid/ask")
        if self.mark_price is not None:
            finite(self.mark_price, "mark_price", ZERO)
            if self.mark_price == ZERO:
                raise TradingError("mark_price must be positive")
        if self.funding_rate is not None:
            finite(self.funding_rate, "funding_rate")
        if self.next_funding_at is not None:
            aware(self.next_funding_at, "next_funding_at")


@dataclass(frozen=True, slots=True)
class PaperOrder:
    client_order_id: str
    instrument_id: str
    portfolio_id: str
    side: str
    quantity: Decimal
    fee_bps: Decimal
    leverage: Decimal = ONE
    reduce_only: bool = False

    def __post_init__(self) -> None:
        for name in ("client_order_id", "instrument_id", "portfolio_id"):
            identifier(getattr(self, name), name)
        if self.side not in {"buy", "sell"}:
            raise TradingError("side must be buy or sell")
        finite(self.quantity, "quantity", ZERO)
        if self.quantity == ZERO:
            raise TradingError("quantity must be positive")
        finite(self.fee_bps, "fee_bps", ZERO)
        if self.fee_bps >= Decimal("10000"):
            raise TradingError("fee_bps must be < 10000")
        finite(self.leverage, "leverage", ONE)
        if not isinstance(self.reduce_only, bool):
            raise TradingError("reduce_only must be boolean")


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    instrument_id: str
    portfolio_id: str
    observed_at: datetime
    quote_observed_at: datetime
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    fee_model: str = "explicit_flat_bps_of_notional"
    execution_model: str = "paper_top_of_book_full_fill_no_queue_or_market_impact"


@dataclass(frozen=True, slots=True)
class Position:
    instrument_id: str
    portfolio_id: str
    quantity: Decimal = ZERO
    average_price: Decimal = ZERO
    leverage: Decimal = ONE
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    observed_at: datetime | None = None
