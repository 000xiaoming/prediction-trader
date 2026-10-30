"""Evaluate an explicit paper order against a detached account without recording it."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from prediction_trader.trading.engine import PaperAccount
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import (
    InstrumentSpec,
    PaperOrder,
    Quote,
    TradingError,
    jsonable,
)


def preview_order(
    path: str | Path, spec: InstrumentSpec, order: PaperOrder, quote: Quote,
    *, initial_cash: Decimal = Decimal("1000"), now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    account = SQLitePaperLedger(path, read_only=True).account(order.portfolio_id)
    exists = account is not None
    if account is None:
        account = PaperAccount(order.portfolio_id, initial_cash, spec.quote_currency)
    if order.client_order_id in account.orders:
        raise TradingError(
            "Order ID is already recorded; choose a new ID for a hypothetical preview",
        )
    before = account.summary()
    fill = account.execute(spec, order, quote, now=now)
    return {
        "mode": "paper", "preview": True, "persisted": False, "account_exists": exists,
        "evaluated_at": now.isoformat(), "quote": jsonable(quote), "fill": jsonable(fill),
        "account_before": before, "account_after": account.summary(),
    }
