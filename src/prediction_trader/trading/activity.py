"""Read-only activity totals from recorded events; not a portfolio return calculation."""

from collections.abc import Iterable
from contextlib import closing
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, cast

from prediction_trader.trading.ledger import EVENT_TYPES, SQLitePaperLedger
from prediction_trader.trading.models import TradingError, identifier, jsonable

_AMOUNTS = (
    "buy_quantity", "sell_quantity", "buy_notional", "sell_notional", "fees",
    "fill_realized_pnl", "funding_payments", "settlement_payouts",
)


def _number(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise TradingError("Activity requires decimal strings in recorded events")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise TradingError("Activity contains an invalid decimal") from exc
    if not number.is_finite():
        raise TradingError("Activity contains a non-finite decimal")
    return number


def _arithmetic(left: Decimal, right: Decimal, *, multiply: bool = False) -> Decimal:
    # Preserve low-order digits when summing long histories or multiplying precise fills.
    with localcontext() as context:
        exponent = min(cast(int, left.as_tuple().exponent), cast(int, right.as_tuple().exponent))
        context.prec = max(28, len(left.as_tuple().digits) + len(right.as_tuple().digits),
                           max(left.adjusted(), right.adjusted()) - exponent + 2)
        return left * right if multiply else left + right


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    counts = dict.fromkeys(EVENT_TYPES, 0)
    groups: dict[str, dict[str, Any]] = {}
    first = last = None
    for event in events:
        try:
            kind, payload = event["event_type"], event["payload"]
            if kind not in counts or not isinstance(payload, dict):
                raise TradingError("Unsupported activity event")
            fill = payload["result"] if kind == "fill" else {}
            if kind == "fill":
                instrument = fill["instrument_id"]
            elif kind == "settlement":
                instrument = payload["instrument_id"]
            else:
                instrument = payload["quote"]["instrument_id"]
            identifier(instrument, "instrument_id")
            group = groups.setdefault(instrument, {
                "instrument_id": instrument, "quote_currency": None,
                "events_by_type": dict.fromkeys(EVENT_TYPES, 0),
                **dict.fromkeys(_AMOUNTS, Decimal("0")),
            })
            counts[kind] += 1
            group["events_by_type"][kind] += 1
            first = event["sequence"] if first is None else first
            last = event["sequence"]
            if kind == "fill":
                currency = payload["instrument"]["quote_currency"]
                identifier(currency, "quote_currency")
                if group["quote_currency"] not in (None, currency):
                    raise TradingError("Conflicting activity quote currencies")
                group["quote_currency"] = currency
                side = fill["side"]
                if side not in ("buy", "sell"):
                    raise TradingError("Unsupported activity fill side")
                quantity, price, fee = (_number(fill[key]) for key in ("quantity", "price", "fee"))
                if quantity <= 0 or price <= 0 or fee < 0:
                    raise TradingError("Invalid recorded fill amounts")
                amounts = {side + "_quantity": quantity,
                           side + "_notional": _arithmetic(quantity, price, multiply=True),
                           "fees": fee, "fill_realized_pnl": _number(fill["realized_pnl"])}
            elif kind in ("funding", "settlement"):
                key = "funding_payments" if kind == "funding" else "settlement_payouts"
                amounts = {key: _number(payload["result"])}
            else:
                amounts = {}
            for key, value in amounts.items():
                group[key] = _arithmetic(group[key], value)
        except (KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            raise TradingError("Activity contains an incomplete recorded event") from exc
    return cast(dict[str, Any], jsonable({
        "event_count": sum(counts.values()), "events_by_type": counts,
        "first_sequence": first, "last_sequence": last,
        "instruments": [groups[key] for key in sorted(groups)],
    }))


def activity_report(
    ledger: SQLitePaperLedger, portfolio_id: str,
    *, since: str | None = None, until: str | None = None,
    max_events: int | None = None, timeout_seconds: float | None = None,
) -> dict[str, Any]:
    with closing(ledger.iter_history(
        portfolio_id, since=since, until=until,
        max_events=max_events, timeout_seconds=timeout_seconds,
    )) as events:
        totals = summarize_events(events)
    return {"mode": "paper", "basis": "recorded_events", "portfolio_id": portfolio_id,
            "since": since, "until": until, **totals}
