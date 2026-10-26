"""Bounded, persistent HTTP clients for public data. No order/account endpoints."""

import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any

import httpx

from prediction_trader.trading.catalog import event_instruments, get_instrument
from prediction_trader.trading.models import (
    InstrumentKind,
    InstrumentSpec,
    Quote,
    TradingError,
    finite,
    identifier,
)


class PublicMarketData:
    """Read-only spot, USDT linear perpetual, and ordinary YES/NO event quotes."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout_seconds: float = 8,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise TradingError("timeout_seconds must be in (0, 30]")
        self._owned = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._timeout = timeout_seconds
        self._rules: dict[str, tuple[float, InstrumentSpec]] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> "PublicMarketData":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def _get(self, base: str, path: str, **params: str) -> dict[str, Any]:
        try:
            response = self._client.get(
                base + path,
                params=params,
                timeout=self._timeout,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            raise TradingError(f"Public data unavailable ({status or type(exc).__name__})") from exc
        except ValueError as exc:
            raise TradingError("Public data response is not JSON") from exc
        if not isinstance(payload, dict):
            raise TradingError("Public data response must be an object")
        return payload

    def snapshot(
        self,
        instrument_id: str,
        portfolio_id: str = "default",
    ) -> tuple[InstrumentSpec, Quote]:
        identifier(portfolio_id, "portfolio_id")
        event = re.fullmatch(r"polymarket_event_([0-9]{1,30})_(yes|no)", instrument_id)
        if event:
            return self.event_snapshot(event.group(1), event.group(2).upper(), portfolio_id)
        spec = get_instrument(instrument_id)
        try:
            return self._binance_snapshot(spec, portfolio_id)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            if isinstance(exc, TradingError):
                raise
            raise TradingError("Invalid Binance public market data") from exc

    def _binance_rules(self, original: InstrumentSpec) -> InstrumentSpec:
        with self._lock:
            entry = self._rules.get(original.instrument_id)
            if entry is not None and time.monotonic() - entry[0] < 300:
                return entry[1]
            perpetual = original.kind == InstrumentKind.PERPETUAL
            base = "https://fapi.binance.com" if perpetual else "https://data-api.binance.vision"
            prefix = "/fapi/v1" if perpetual else "/api/v3"
            params = {} if perpetual else {"symbol": original.symbol}
            response = self._get(base, prefix + "/exchangeInfo", **params)
            symbols = response["symbols"]
            matches = [item for item in symbols if item.get("symbol") == original.symbol]
            if len(matches) != 1:
                raise TradingError("Instrument missing from exchangeInfo")
            info = matches[0]
            if (
                info.get("status") != "TRADING"
                or info.get("baseAsset") != original.base_currency
                or info.get("quoteAsset") != original.quote_currency
                or (perpetual and info.get("contractType") != "PERPETUAL")
            ):
                raise TradingError("Instrument metadata does not match the active catalog")
            filters = {item["filterType"]: item for item in info["filters"]}
            tick = Decimal(str(filters["PRICE_FILTER"]["tickSize"]))
            lot = filters["LOT_SIZE"]
            step = Decimal(str(lot["stepSize"]))
            minimum = Decimal(str(lot["minQty"]))
            maximum = Decimal(str(lot["maxQty"]))
            market_lot = filters.get("MARKET_LOT_SIZE")
            if market_lot:
                market_step = Decimal(str(market_lot["stepSize"]))
                if market_step > 0:
                    step = max(step, market_step)
                minimum = max(minimum, Decimal(str(market_lot["minQty"])))
                market_max = Decimal(str(market_lot["maxQty"]))
                if market_max > 0:
                    maximum = min(maximum, market_max)
            notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
            if not notional_filter:
                raise TradingError("exchangeInfo is missing the minimum notional filter")
            notional = Decimal(
                str(notional_filter.get("minNotional", notional_filter.get("notional")))
            )
            spec = replace(
                original,
                price_tick=tick,
                quantity_step=step,
                minimum_quantity=minimum,
                minimum_notional=notional,
                maximum_quantity=maximum,
                rules_source="exchangeInfo_with_conservative_paper_leverage_and_margin_limits",
            )
            self._rules[original.instrument_id] = (time.monotonic(), spec)
            return spec

    def _binance_snapshot(
        self,
        original: InstrumentSpec,
        portfolio_id: str,
    ) -> tuple[InstrumentSpec, Quote]:
        spec = self._binance_rules(original)
        perpetual = spec.kind == InstrumentKind.PERPETUAL
        base = "https://fapi.binance.com" if perpetual else "https://data-api.binance.vision"
        prefix = "/fapi/v1" if perpetual else "/api/v3"
        requested_at = datetime.now(UTC)
        book = self._get(base, prefix + "/ticker/bookTicker", symbol=spec.symbol)
        if book.get("symbol") != spec.symbol:
            raise TradingError("Binance book symbol mismatch")
        observed_at, basis = requested_at, "request_started_at_no_exchange_timestamp"
        mark, rate, next_funding = None, None, None
        if perpetual:
            premium = self._get(base, prefix + "/premiumIndex", symbol=spec.symbol)
            if premium.get("symbol") != spec.symbol:
                raise TradingError("Binance mark symbol mismatch")
            observed_at = min(_milliseconds(book["time"]), _milliseconds(premium["time"]))
            basis = "oldest_exchange_book_and_mark_timestamp"
            mark = Decimal(str(premium["markPrice"]))
            rate = Decimal(str(premium["lastFundingRate"]))
            next_funding = _milliseconds(premium["nextFundingTime"])
        quote = Quote(
            spec.instrument_id,
            portfolio_id,
            observed_at,
            Decimal(str(book["bidPrice"])),
            Decimal(str(book["askPrice"])),
            Decimal(str(book["bidQty"])),
            Decimal(str(book["askQty"])),
            mark,
            rate,
            next_funding,
            source="binance_public_rest",
            timestamp_basis=basis,
        )
        return spec, quote

    def event_snapshot(
        self,
        market_id: str,
        outcome: str,
        portfolio_id: str = "default",
    ) -> tuple[InstrumentSpec, Quote]:
        if not re.fullmatch(r"[0-9]{1,30}", market_id) or outcome.upper() not in {"YES", "NO"}:
            raise TradingError("Select a numeric Gamma market ID and YES or NO")
        identifier(portfolio_id, "portfolio_id")
        try:
            payload = self.event_market(market_id)
            candidates = event_instruments(payload)
            spec = next(item for item in candidates if item.outcome == outcome.upper())
            if spec.expires_at is not None and datetime.now(UTC) >= spec.expires_at:
                raise TradingError("Event market has expired")
            book = self._get("https://clob.polymarket.com", "/book", token_id=str(spec.token_id))
            if (
                str(book.get("asset_id")) != spec.token_id
                or str(book.get("market")).lower() != str(spec.condition_id).lower()
            ):
                raise TradingError("CLOB book does not match the selected market/token")
            bids, asks = _levels(book["bids"]), _levels(book["asks"])
            if not bids or not asks:
                raise TradingError("Event order book has no two-sided liquidity")
            bid = max(price for price, _ in bids)
            ask = min(price for price, _ in asks)
            spec = replace(
                spec,
                price_tick=Decimal(str(book["tick_size"])),
                minimum_quantity=Decimal(str(book["min_order_size"])),
                rules_source="gamma_and_clob_metadata_with_paper_0.01_share_precision",
            )
            quote = Quote(
                spec.instrument_id,
                portfolio_id,
                _milliseconds(book["timestamp"]),
                bid,
                ask,
                sum((size for price, size in bids if price == bid), Decimal(0)),
                sum((size for price, size in asks if price == ask), Decimal(0)),
                source="polymarket_public_clob",
                timestamp_basis="exchange_book_timestamp",
            )
            return spec, quote
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            if isinstance(exc, TradingError):
                raise
            raise TradingError("Invalid Polymarket public market data") from exc

    def event_market(self, market_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9]{1,30}", market_id):
            raise TradingError("Gamma market ID must be numeric")
        payload = self._get("https://gamma-api.polymarket.com", f"/markets/{market_id}")
        if str(payload.get("id")) != market_id:
            raise TradingError("Gamma returned a different market")
        return payload


def _milliseconds(value: object) -> datetime:
    milliseconds = Decimal(str(value))
    finite(milliseconds, "exchange timestamp", Decimal(0))
    return datetime.fromtimestamp(float(milliseconds / 1000), UTC)


def _levels(raw: object) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(raw, list):
        raise TradingError("Invalid CLOB levels")
    levels = []
    for item in raw:
        if not isinstance(item, dict):
            raise TradingError("Invalid CLOB level")
        price, size = Decimal(str(item["price"])), Decimal(str(item["size"]))
        finite(price, "book price", Decimal(0))
        finite(size, "book size", Decimal(0))
        if price > 1:
            raise TradingError("Binary book price exceeds 1")
        if size > 0 and price > 0:
            levels.append((price, size))
    return levels
