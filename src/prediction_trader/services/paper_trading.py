"""Public quotes and an explicitly simulated, persistent execution gateway."""

import re
import threading
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any

from prediction_trader.config import Settings
from prediction_trader.services.snapshot_cache import SnapshotCache
from prediction_trader.trading.adapters import PublicMarketData
from prediction_trader.trading.catalog import INSTRUMENTS
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import (
    InstrumentSpec,
    PaperOrder,
    Quote,
    TradingError,
    jsonable,
)


class PaperTradingGateway:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._data = PublicMarketData()
        self._ledger: SQLitePaperLedger | None = None
        self._lock = threading.RLock()
        self._quotes: OrderedDict[str, SnapshotCache[tuple[InstrumentSpec, Quote]]] = OrderedDict()

    @staticmethod
    def instruments() -> dict[str, Any]:
        return {"mode": "paper", "instruments": [
            {**jsonable(spec), "label": (
                f"{spec.base_currency} USDT " + ("永续" if spec.kind == "perpetual" else "现货")
            )} for spec in INSTRUMENTS.values()
        ]}

    @staticmethod
    def portfolio(instrument_id: str) -> str:
        if instrument_id not in INSTRUMENTS and not re.fullmatch(
            r"polymarket_event_[0-9]{1,30}_(yes|no)", instrument_id,
        ):
            raise TradingError("Unknown instrument")
        return "paper:" + instrument_id

    @property
    def ledger(self) -> SQLitePaperLedger:
        if self._ledger is None:
            self._ledger = SQLitePaperLedger(self._settings.paper_ledger_path)
        return self._ledger

    def state(self, instrument_id: str) -> dict[str, Any]:
        portfolio = self.portfolio(instrument_id)
        with self._lock:
            cache = self._quotes.get(instrument_id)
            if cache is None:
                cache = SnapshotCache(
                    lambda: self._data.snapshot(instrument_id, portfolio), ttl_seconds=2,
                )
                self._quotes[instrument_id] = cache
                if len(self._quotes) > 64:
                    self._quotes.popitem(last=False)
            self._quotes.move_to_end(instrument_id)
            spec, quote = cache.read()
            account = self.ledger.account(portfolio)
            if account is not None:
                account = self.ledger.mark(quote)
            return {
                "mode": "paper", "instrument": jsonable(spec), "quote": jsonable(quote),
                "account": account.summary() if account else None,
                "portfolio_id": portfolio, "max_quote_age_seconds": 10,
                "initial_cash": str(self._settings.paper_initial_cash),
                "funding_mode": "explicit_ledger_events",
            }

    def order(self, payload: Any) -> dict[str, Any]:
        required = {"instrument_id", "client_order_id", "side", "quantity", "fee_bps"}
        allowed = required | {"leverage", "reduce_only"}
        if not isinstance(payload, dict) or not required <= payload.keys() <= allowed:
            raise TradingError("Invalid paper order fields; explicit fee_bps is required")
        if any(not isinstance(payload[key], str) for key in required):
            raise TradingError("Order identifiers, quantity and fee_bps must be strings")
        if "leverage" in payload and not isinstance(payload["leverage"], str):
            raise TradingError("leverage must be a decimal string")
        portfolio = self.portfolio(payload["instrument_id"])
        try:
            order = PaperOrder(
                client_order_id=payload["client_order_id"], instrument_id=payload["instrument_id"],
                portfolio_id=portfolio, side=payload["side"], quantity=Decimal(payload["quantity"]),
                fee_bps=Decimal(payload["fee_bps"]), leverage=Decimal(payload.get("leverage", "1")),
                reduce_only=payload.get("reduce_only", False),
            )
        except InvalidOperation as exc:
            raise TradingError("Invalid decimal in order") from exc
        with self._lock:
            account = self.ledger.account(portfolio)
            prior = account.orders.get(order.client_order_id) if account else None
            if prior is not None:
                if prior[0] != order:
                    raise TradingError(
                        "Idempotency conflict: client_order_id has different payload",
                    )
                assert account is not None
                return {"mode": "paper", "fill": jsonable(prior[1]), "account": account.summary()}
            spec, quote = self._data.snapshot(order.instrument_id, portfolio)
            fill, account = self.ledger.execute(
                spec, order, quote, initial_cash=self._settings.paper_initial_cash,
            )
            self._quotes.pop(order.instrument_id, None)
            return {"mode": "paper", "fill": jsonable(fill), "account": account.summary()}

    def history(
        self, instrument_id: str, *, limit: int = 50, before: int | None = None,
        event_type: str | None = "fill",
    ) -> dict[str, Any]:
        portfolio = self.portfolio(instrument_id)
        ledger = SQLitePaperLedger(self._settings.paper_ledger_path, read_only=True)
        return {
            "mode": "paper", "instrument_id": instrument_id,
            **ledger.history_page(portfolio, limit=limit, before=before, event_type=event_type),
        }

    def close(self) -> None:
        self._data.close()
