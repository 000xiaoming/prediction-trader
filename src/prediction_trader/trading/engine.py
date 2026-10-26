"""Deterministic paper accounting, not an exchange-exact matching/liquidation model.

Spot/event purchases consume cash. Linear perpetuals reserve margin, realize P&L on
reductions, and book explicit funding separately. Liquidation risk halts the account;
the simulator never invents liquidation fills or calls an authenticated endpoint.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from prediction_trader.trading.catalog import gamma_arrays
from prediction_trader.trading.models import (
    ONE,
    ZERO,
    Fill,
    InstrumentKind,
    InstrumentSpec,
    PaperOrder,
    Position,
    Quote,
    TradingError,
    aware,
    finite,
    identifier,
    jsonable,
)


class PaperAccount:
    def __init__(
        self,
        portfolio_id: str,
        cash: Decimal,
        currency: str = "USDT",
        max_quote_age_seconds: int = 10,
    ) -> None:
        identifier(portfolio_id, "portfolio_id")
        identifier(currency, "currency")
        finite(cash, "initial cash", ZERO)
        if not 1 <= max_quote_age_seconds <= 300:
            raise TradingError("max_quote_age_seconds must be in [1, 300]")
        self.portfolio_id = portfolio_id
        self.currency = currency
        self.initial_cash = cash
        self.cash = cash
        self.max_quote_age_seconds = max_quote_age_seconds
        self.positions: dict[str, Position] = {}
        self.specs: dict[str, InstrumentSpec] = {}
        self.quotes: dict[str, Quote] = {}
        self.orders: dict[str, tuple[PaperOrder, Fill]] = {}
        self.funding_events: dict[str, dict[str, Any]] = {}
        self.settlements: dict[str, Decimal] = {}
        self.liquidity_used: dict[str, Decimal] = {}
        self.halted_reason: str | None = None

    def _quote(self, spec: InstrumentSpec, quote: Quote, now: datetime) -> None:
        aware(now, "now")
        if quote.portfolio_id != self.portfolio_id or quote.instrument_id != spec.instrument_id:
            raise TradingError("Quote instrument/portfolio mismatch")
        if spec.quote_currency != self.currency:
            raise TradingError("Currency mismatch; use a separate portfolio")
        registered = self.specs.get(spec.instrument_id)
        if registered is not None and any(
            getattr(registered, field) != getattr(spec, field)
            for field in (
                "venue",
                "kind",
                "symbol",
                "base_currency",
                "quote_currency",
                "market_id",
                "condition_id",
                "token_id",
                "outcome",
            )
        ):
            raise TradingError("Instrument identity changed for an existing position")
        age = (now - quote.observed_at).total_seconds()
        if age < -1 or age > self.max_quote_age_seconds:
            raise TradingError("Quote is stale or from the future")
        previous = self.quotes.get(spec.instrument_id)
        if previous and quote.observed_at < previous.observed_at:
            raise TradingError("Quote is older than the last account observation")
        if spec.kind == InstrumentKind.BINARY_EVENT and quote.ask > ONE:
            raise TradingError("Binary quote price must be <= 1")
        if quote.bid % spec.price_tick or quote.ask % spec.price_tick:
            raise TradingError("Quote violates price precision")
        if spec.kind == InstrumentKind.PERPETUAL and quote.mark_price is None:
            raise TradingError("Perpetual requires an explicit mark price")

    def _valuation(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        spot_value = ZERO
        perpetual_pnl = ZERO
        margin = ZERO
        maintenance = ZERO
        for key, position in self.positions.items():
            if position.quantity == ZERO:
                continue
            spec, quote = self.specs[key], self.quotes[key]
            if spec.kind == InstrumentKind.PERPETUAL:
                assert quote.mark_price is not None
                perpetual_pnl += position.quantity * (quote.mark_price - position.average_price)
                # Reserve on the higher of entry and current mark, without using spot as collateral.
                margin += (
                    abs(position.quantity)
                    * max(quote.mark_price, position.average_price)
                    / position.leverage
                )
                maintenance += (
                    abs(position.quantity) * quote.mark_price * (spec.maintenance_margin_rate)
                )
            else:
                spot_value += position.quantity * quote.bid
        return spot_value, perpetual_pnl, margin, maintenance

    def summary(self) -> dict[str, Any]:
        spot_value, perpetual_pnl, margin, maintenance = self._valuation()
        spot_unrealized = sum(
            (
                position.quantity * (self.quotes[key].bid - position.average_price)
                for key, position in self.positions.items()
                if self.specs[key].kind != InstrumentKind.PERPETUAL
            ),
            ZERO,
        )
        return cast(
            dict[str, Any],
            jsonable(
                {
                    "portfolio_id": self.portfolio_id,
                    "currency": self.currency,
                    "mode": "paper",
                    "live_execution": False,
                    "cash": self.cash,
                    "initial_cash": self.initial_cash,
                    "equity": self.cash + spot_value + perpetual_pnl,
                    "unrealized_pnl": spot_unrealized + perpetual_pnl,
                    "realized_pnl": sum((p.realized_pnl for p in self.positions.values()), ZERO),
                    "fees": sum((p.fees for p in self.positions.values()), ZERO),
                    "funding": sum((p.funding for p in self.positions.values()), ZERO),
                    "margin": margin,
                    "maintenance_margin": maintenance,
                    "available_margin": self.cash + perpetual_pnl - margin,
                    "halted_reason": self.halted_reason,
                    "fill_count": len(self.orders),
                    "positions": list(self.positions.values()),
                    "valuation_observed_at": {key: q.observed_at for key, q in self.quotes.items()},
                    "valuation_note": "Last observed marks; refresh before using current equity.",
                }
            ),
        )

    def _check_halt(self) -> None:
        _, perpetual_pnl, _, maintenance = self._valuation()
        if maintenance > ZERO and self.cash + perpetual_pnl <= maintenance:
            self.halted_reason = "maintenance_margin_breach_manual_review_required"
        elif self.cash < ZERO:
            self.halted_reason = "negative_cash_manual_review_required"

    def execute(
        self,
        spec: InstrumentSpec,
        order: PaperOrder,
        quote: Quote,
        *,
        now: datetime | None = None,
    ) -> Fill:
        now = now or datetime.now(UTC)
        if order.portfolio_id != self.portfolio_id or order.instrument_id != spec.instrument_id:
            raise TradingError("Order instrument/portfolio mismatch")
        prior = self.orders.get(order.client_order_id)
        if prior is not None:
            if prior[0] != order:
                raise TradingError("Idempotency conflict: client_order_id has different payload")
            return prior[1]
        if self.halted_reason:
            raise TradingError(f"Account halted: {self.halted_reason}")
        self._quote(spec, quote, now)
        if spec.instrument_id in self.settlements:
            raise TradingError("Event instrument is already settled")
        if spec.expires_at is not None and now >= spec.expires_at:
            raise TradingError("Instrument has expired; wait for verified settlement")
        if order.leverage > spec.maximum_leverage:
            raise TradingError("Instrument leverage limit exceeded")
        if order.quantity % spec.quantity_step:
            raise TradingError("Order violates quantity precision")
        if order.quantity < spec.minimum_quantity:
            raise TradingError("Order is below minimum quantity")
        if spec.maximum_quantity is not None and order.quantity > spec.maximum_quantity:
            raise TradingError("Order exceeds maximum quantity")
        price = quote.ask if order.side == "buy" else quote.bid
        size = quote.ask_size if order.side == "buy" else quote.bid_size
        liquidity_key = (
            f"{spec.instrument_id}:{quote.observed_at.isoformat()}:"
            f"{quote.bid.normalize()}:{quote.ask.normalize()}:{order.side}"
        )
        consumed = self.liquidity_used.get(liquidity_key, ZERO)
        if order.quantity + consumed > size:
            raise TradingError("Insufficient displayed liquidity; partial fills are not assumed")
        if order.quantity * price < spec.minimum_notional:
            raise TradingError("Order is below minimum notional")
        for key, position in self.positions.items():
            if (
                key != spec.instrument_id
                and position.quantity != ZERO
                and self.specs[key].kind == InstrumentKind.PERPETUAL
            ):
                self._quote(self.specs[key], self.quotes[key], now)
        previous = self.positions.get(
            spec.instrument_id, Position(spec.instrument_id, self.portfolio_id)
        )
        if previous.quantity and previous.leverage != order.leverage:
            raise TradingError("Leverage cannot change while a position is open")
        delta = order.quantity if order.side == "buy" else -order.quantity
        new_quantity = previous.quantity + delta
        reducing = previous.quantity * delta < ZERO
        if order.reduce_only and (not reducing or order.quantity > abs(previous.quantity)):
            raise TradingError("reduce_only cannot open, enlarge, or reverse a position")
        if spec.kind != InstrumentKind.PERPETUAL and new_quantity < ZERO:
            raise TradingError("Spot/event positions cannot be naked short")
        if spec.kind != InstrumentKind.PERPETUAL and order.leverage != ONE:
            raise TradingError("Spot/event positions cannot use leverage")
        closed_quantity = min(abs(previous.quantity), order.quantity) if reducing else ZERO
        direction = ONE if previous.quantity >= ZERO else -ONE
        realized = closed_quantity * (price - previous.average_price) * direction
        if new_quantity == ZERO:
            average = ZERO
        elif previous.quantity == ZERO or (reducing and order.quantity > abs(previous.quantity)):
            average = price
        elif reducing:
            average = previous.average_price
        else:
            average = (
                abs(previous.quantity) * previous.average_price + order.quantity * price
            ) / abs(new_quantity)
        fee = order.quantity * price * order.fee_bps / Decimal("10000")
        new_cash = self.cash - fee
        new_cash += realized if spec.kind == InstrumentKind.PERPETUAL else -delta * price
        candidate = replace(
            previous,
            quantity=new_quantity,
            average_price=average,
            leverage=order.leverage,
            realized_pnl=previous.realized_pnl + realized,
            fees=previous.fees + fee,
            observed_at=now,
        )
        # Work on a candidate account so every rejection leaves cash and positions unchanged.
        preview = PaperAccount(
            self.portfolio_id, self.initial_cash, self.currency, self.max_quote_age_seconds
        )
        preview.positions = dict(self.positions)
        preview.specs = dict(self.specs)
        preview.quotes = dict(self.quotes)
        preview.cash = new_cash
        preview.positions[spec.instrument_id] = candidate
        preview.specs[spec.instrument_id] = spec
        preview.quotes[spec.instrument_id] = quote
        _, pnl, margin, maintenance = preview._valuation()
        if new_cash < ZERO or new_cash + pnl < margin:
            raise TradingError("Insufficient cash or margin")
        if maintenance and new_cash + pnl <= maintenance:
            raise TradingError("Order would breach maintenance margin")
        fill = Fill(
            order.client_order_id,
            spec.instrument_id,
            self.portfolio_id,
            now,
            quote.observed_at,
            order.side,
            order.quantity,
            price,
            fee,
            realized,
        )
        self.cash = new_cash
        self.positions[spec.instrument_id] = candidate
        self.specs[spec.instrument_id] = spec
        self.quotes[spec.instrument_id] = quote
        self.orders[order.client_order_id] = (order, fill)
        self.liquidity_used[liquidity_key] = consumed + order.quantity
        return fill

    def mark(self, quote: Quote, *, now: datetime | None = None) -> None:
        spec = self.specs.get(quote.instrument_id)
        if spec is None:
            raise TradingError("Cannot mark an unknown instrument")
        self._quote(spec, quote, now or datetime.now(UTC))
        self.quotes[quote.instrument_id] = quote
        self._check_halt()

    def apply_funding(
        self,
        payment_id: str,
        quote: Quote,
        rate: Decimal,
        *,
        now: datetime | None = None,
    ) -> Decimal:
        """Apply an explicitly observed funding event once, never a forecast rate automatically."""
        identifier(payment_id, "payment_id")
        finite(rate, "funding rate")
        if abs(rate) > ONE:
            raise TradingError("Funding rate magnitude exceeds 1")
        record = {"quote": jsonable(quote), "rate": str(rate)}
        old = self.funding_events.get(payment_id)
        if old is not None:
            if old["record"] != record:
                raise TradingError("Idempotency conflict: funding payment")
            return Decimal(old["payment"])
        spec = self.specs.get(quote.instrument_id)
        if spec is None or spec.kind != InstrumentKind.PERPETUAL:
            raise TradingError("Funding requires an existing perpetual position")
        self._quote(spec, quote, now or datetime.now(UTC))
        position = self.positions[quote.instrument_id]
        if position.observed_at is not None and quote.observed_at < position.observed_at:
            raise TradingError("Funding predates the latest position change")
        assert quote.mark_price is not None
        payment = -position.quantity * quote.mark_price * rate
        self.cash += payment
        self.positions[quote.instrument_id] = replace(
            position,
            funding=position.funding + payment,
            observed_at=quote.observed_at,
        )
        self.quotes[quote.instrument_id] = quote
        self.funding_events[payment_id] = {"record": record, "payment": str(payment)}
        self._check_halt()
        return payment

    def settle_event(
        self,
        instrument_id: str,
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> Decimal:
        spec = self.specs.get(instrument_id)
        if spec is None or spec.kind != InstrumentKind.BINARY_EVENT:
            raise TradingError("Settlement requires an existing event instrument")
        labels, tokens = gamma_arrays(payload)
        if (
            str(payload.get("id")) != spec.market_id
            or payload.get("conditionId") != spec.condition_id
            or tokens[labels.index(str(spec.outcome))] != spec.token_id
        ):
            raise TradingError("Settlement market/token mapping mismatch")
        if payload.get("closed") is not True or payload.get("umaResolutionStatus") != "resolved":
            raise TradingError("Settlement requires a final resolved Gamma market")
        try:
            raw_prices = payload["outcomePrices"]
            raw_prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            prices = [Decimal(str(price)) for price in raw_prices]
            resolved_at = datetime.fromisoformat(str(payload["closedTime"]).replace("Z", "+00:00"))
        except (ValueError, InvalidOperation, TypeError, KeyError) as exc:
            raise TradingError("Invalid settlement prices/time") from exc
        aware(resolved_at, "resolved_at")
        if (
            len(prices) != 2
            or any(not price.is_finite() for price in prices)
            or set(prices) != {ZERO, ONE}
        ):
            raise TradingError("Settlement requires exactly 0/1 outcome prices")
        now = now or datetime.now(UTC)
        aware(now, "now")
        position = self.positions[instrument_id]
        if resolved_at > now or (
            position.observed_at is not None and resolved_at < position.observed_at
        ):
            raise TradingError("Settlement timestamp is outside position history")
        payout_price = prices[labels.index(str(spec.outcome))]
        if instrument_id in self.settlements:
            if self.settlements[instrument_id] != payout_price:
                raise TradingError("Conflicting event settlement")
            return ZERO
        payout = position.quantity * payout_price
        realized = position.quantity * (payout_price - position.average_price)
        self.cash += payout
        self.positions[instrument_id] = replace(
            position,
            quantity=ZERO,
            average_price=ZERO,
            realized_pnl=position.realized_pnl + realized,
            observed_at=resolved_at,
        )
        self.settlements[instrument_id] = payout_price
        return payout

    def to_state(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            jsonable(
                {
                    "version": 1,
                    "portfolio_id": self.portfolio_id,
                    "currency": self.currency,
                    "initial_cash": self.initial_cash,
                    "cash": self.cash,
                    "max_quote_age_seconds": self.max_quote_age_seconds,
                    "positions": self.positions,
                    "specs": self.specs,
                    "quotes": self.quotes,
                    "orders": {
                        key: {"order": order, "fill": fill}
                        for key, (order, fill) in self.orders.items()
                    },
                    "funding_events": self.funding_events,
                    "settlements": self.settlements,
                    "liquidity_used": self.liquidity_used,
                    "halted_reason": self.halted_reason,
                }
            ),
        )

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "PaperAccount":
        if state.get("version") != 1:
            raise TradingError("Unsupported paper ledger state version")
        account = cls(
            state["portfolio_id"],
            Decimal(state["initial_cash"]),
            state["currency"],
            state["max_quote_age_seconds"],
        )
        account.cash = Decimal(state["cash"])
        for key, raw in state["specs"].items():
            values = dict(raw)
            values["kind"] = InstrumentKind(values["kind"])
            for field in (
                "price_tick",
                "quantity_step",
                "minimum_quantity",
                "minimum_notional",
                "maximum_leverage",
                "maintenance_margin_rate",
                "maximum_quantity",
            ):
                if values[field] is not None:
                    values[field] = Decimal(values[field])
            if values["expires_at"] is not None:
                values["expires_at"] = datetime.fromisoformat(values["expires_at"])
            account.specs[key] = InstrumentSpec(**values)
        for key, raw in state["positions"].items():
            values = dict(raw)
            for field in (
                "quantity",
                "average_price",
                "leverage",
                "realized_pnl",
                "fees",
                "funding",
            ):
                values[field] = Decimal(values[field])
            if values["observed_at"] is not None:
                values["observed_at"] = datetime.fromisoformat(values["observed_at"])
            account.positions[key] = Position(**values)
        for key, raw in state["quotes"].items():
            values = dict(raw)
            for field in ("bid", "ask", "bid_size", "ask_size", "mark_price", "funding_rate"):
                if values[field] is not None:
                    values[field] = Decimal(values[field])
            for field in ("observed_at", "next_funding_at"):
                if values[field] is not None:
                    values[field] = datetime.fromisoformat(values[field])
            account.quotes[key] = Quote(**values)
        for key, raw in state["orders"].items():
            order, fill = dict(raw["order"]), dict(raw["fill"])
            for field in ("quantity", "fee_bps", "leverage"):
                order[field] = Decimal(order[field])
            for field in ("quantity", "price", "fee", "realized_pnl"):
                fill[field] = Decimal(fill[field])
            for field in ("observed_at", "quote_observed_at"):
                fill[field] = datetime.fromisoformat(fill[field])
            account.orders[key] = PaperOrder(**order), Fill(**fill)
        account.funding_events = state["funding_events"]
        account.settlements = {key: Decimal(value) for key, value in state["settlements"].items()}
        account.liquidity_used = {
            key: Decimal(value) for key, value in state.get("liquidity_used", {}).items()
        }
        account.halted_reason = state["halted_reason"]
        return account
