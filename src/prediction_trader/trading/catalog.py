"""Explicit instrument catalog and checked Gamma outcome-to-token mapping."""

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from prediction_trader.trading.models import InstrumentKind, InstrumentSpec, TradingError


def _binance(asset: str, kind: InstrumentKind) -> InstrumentSpec:
    perpetual = kind == InstrumentKind.PERPETUAL
    return InstrumentSpec(
        instrument_id=f"binance_{asset.lower()}_{kind.value}",
        venue="binance",
        kind=kind,
        symbol=f"{asset}USDT",
        base_currency=asset,
        quote_currency="USDT",
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.001") if perpetual else Decimal("0.00001"),
        minimum_quantity=Decimal("0.001") if perpetual else Decimal("0.00001"),
        minimum_notional=Decimal("5"),
        maximum_leverage=Decimal("3") if perpetual else Decimal("1"),
    )


INSTRUMENTS = MappingProxyType(
    {
        spec.instrument_id: spec
        for asset in ("BTC", "ETH")
        for kind in (InstrumentKind.SPOT, InstrumentKind.PERPETUAL)
        for spec in (_binance(asset, kind),)
    }
)


def get_instrument(instrument_id: str) -> InstrumentSpec:
    try:
        return INSTRUMENTS[instrument_id]
    except KeyError as exc:
        raise TradingError(f"Unknown instrument: {instrument_id}") from exc


def gamma_arrays(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    try:
        outcomes = payload.get("outcomes")
        tokens = payload.get("clobTokenIds")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
        if not isinstance(outcomes, list) or not isinstance(tokens, list):
            raise ValueError("Missing arrays")
        if len(outcomes) != 2 or len(tokens) != 2:
            raise ValueError("Not binary")
        if any(not isinstance(item, str) for item in outcomes + tokens):
            raise ValueError("Array values must be strings")
        labels = [item.strip().upper() for item in outcomes]
        if set(labels) != {"YES", "NO"} or len(set(tokens)) != 2:
            raise ValueError("Not ordinary YES/NO or duplicate token")
        if any(not token.isdigit() for token in tokens):
            raise ValueError("Invalid token ID")
        return labels, tokens
    except (ValueError, TypeError) as exc:
        raise TradingError("Invalid Gamma YES/NO token mapping") from exc


def event_instruments(payload: Mapping[str, Any]) -> tuple[InstrumentSpec, InstrumentSpec]:
    """Create two outcome instruments from one explicitly selected active Gamma market."""
    labels, tokens = gamma_arrays(payload)
    if (
        payload.get("active") is not True
        or payload.get("closed") is not False
        or payload.get("enableOrderBook") is not True
        or payload.get("acceptingOrders") is not True
    ):
        raise TradingError("Gamma market is not accepting CLOB orders")
    if payload.get("negRisk") is True:
        raise TradingError("Negative-risk markets need a separate adapter")
    market_id = str(payload.get("id", ""))
    try:
        expires = datetime.fromisoformat(str(payload["endDate"]).replace("Z", "+00:00"))
        tick = Decimal(str(payload["orderPriceMinTickSize"]))
        minimum = Decimal(str(payload["orderMinSize"]))
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise TradingError(
            "Gamma requires endDate, orderPriceMinTickSize, and orderMinSize"
        ) from exc
    by_outcome = {
        label: InstrumentSpec(
            instrument_id=f"polymarket_event_{market_id}_{label.lower()}",
            venue="polymarket",
            kind=InstrumentKind.BINARY_EVENT,
            symbol=f"EVENT-{market_id}-{label}",
            base_currency=label,
            quote_currency="USDC",
            price_tick=tick,
            quantity_step=Decimal("0.01"),
            minimum_quantity=minimum,
            minimum_notional=Decimal("0"),
            market_id=market_id,
            condition_id=str(payload.get("conditionId", "")),
            token_id=token,
            outcome=label,
            title=str(payload.get("question", "")),
            expires_at=expires,
            rules_source="gamma_metadata_with_paper_0.01_share_precision",
        )
        for label, token in zip(labels, tokens, strict=True)
    }
    return by_outcome["YES"], by_outcome["NO"]
