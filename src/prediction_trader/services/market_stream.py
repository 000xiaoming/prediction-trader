import json
import logging
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from prometheus_client import Counter, Gauge
from websockets.sync.client import connect

from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository, OutcomeToken

CONNECTED = Gauge("polymarket_websocket_connected", "Whether the market WebSocket is connected")
MESSAGES = Counter("polymarket_websocket_messages_total", "WebSocket messages", ["event_type"])
RECONNECTS = Counter("polymarket_websocket_reconnects_total", "WebSocket reconnect attempts")
LAST_MESSAGE = Gauge(
    "polymarket_websocket_last_message_unixtime", "Unix time of the last market message"
)
PRICE = Gauge(
    "polymarket_outcome_price",
    "Latest Polymarket outcome price",
    ["market_id", "outcome", "price_type"],
)


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _timestamp(value: object) -> datetime | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        parsed /= 1000
    return datetime.fromtimestamp(float(parsed), tz=UTC)


def _best(levels: object, *, highest: bool) -> Decimal | None:
    if not isinstance(levels, list):
        return None
    prices = [
        price
        for level in levels
        if isinstance(level, dict) and (price := _decimal(level.get("price"))) is not None
    ]
    if not prices:
        return None
    return max(prices) if highest else min(prices)


def _levels(value: object) -> dict[Decimal, Decimal]:
    if not isinstance(value, list):
        return {}
    parsed: dict[Decimal, Decimal] = {}
    for level in value:
        if not isinstance(level, dict):
            continue
        price = _decimal(level.get("price"))
        size = _decimal(level.get("size"))
        if price is not None and size is not None and size > 0:
            parsed[price] = size
    return parsed


class MarketStreamService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop
        self._logger = logging.getLogger("prediction_trader.market_stream")
        self._books: dict[str, tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]] = {}
        self._last_book_metric: dict[str, float] = {}
        self._last_tick_persist: dict[str, float] = {}

    def subscription_tokens(self, repository: MarketRepository) -> list[OutcomeToken]:
        """Union current/next tokens of enabled profiles; exclude historical profiles."""
        by_id: dict[str, OutcomeToken] = {}
        for profile in self._settings.enabled_profiles:
            for token in repository.list_outcome_tokens(profile=profile):
                existing = by_id.get(token.token_id)
                if existing is not None and existing != token:
                    raise ValueError(f"Conflicting Polymarket token identity: {token.token_id}")
                by_id[token.token_id] = token
        return list(by_id.values())

    def run(self) -> None:
        repository = MarketRepository(self._settings.database_url)
        try:
            while not self._stop.is_set():
                try:
                    tokens = self.subscription_tokens(repository)
                    if not tokens:
                        self._stop.wait(2)
                        continue
                    self._consume(repository, tokens)
                except Exception:
                    CONNECTED.set(0)
                    RECONNECTS.inc()
                    self._logger.exception("Polymarket market WebSocket disconnected")
                    self._stop.wait(3)
        finally:
            CONNECTED.set(0)
            repository.close()

    def _consume(self, repository: MarketRepository, tokens: list[OutcomeToken]) -> None:
        by_id = {token.token_id: token for token in tokens}
        # Completed windows should not retain order books/throttle state forever.
        self._books = {key: value for key, value in self._books.items() if key in by_id}
        self._last_book_metric = {
            key: value for key, value in self._last_book_metric.items() if key in by_id
        }
        self._last_tick_persist = {
            key: value for key, value in self._last_tick_persist.items() if key in by_id
        }
        with connect(self._settings.polymarket_market_ws_url, open_timeout=20) as websocket:
            websocket.send(json.dumps({"assets_ids": list(by_id), "type": "market"}))
            CONNECTED.set(1)
            self._logger.info("Subscribed to %d Polymarket outcome tokens", len(by_id))
            last_ping = time.monotonic()
            connected_at = last_ping
            while not self._stop.is_set():
                refresh_after = self._settings.polymarket_websocket_refresh_seconds
                if time.monotonic() - connected_at >= refresh_after:
                    self._logger.info("Refreshing market subscription")
                    return
                if time.monotonic() - last_ping >= 10:
                    websocket.send("PING")
                    last_ping = time.monotonic()
                try:
                    message = websocket.recv(timeout=1)
                except TimeoutError:
                    continue
                if message == "PONG":
                    continue
                self._handle_message(repository, by_id, message)

    def _handle_message(
        self,
        repository: MarketRepository,
        by_id: dict[str, OutcomeToken],
        message: str | bytes,
    ) -> None:
        payload: Any = json.loads(message)
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("event_type", "unknown"))
            MESSAGES.labels(event_type=event_type).inc()
            LAST_MESSAGE.set_to_current_time()
            if event_type == "price_change":
                changes = item.get("price_changes", [])
                if isinstance(changes, list):
                    for change in changes:
                        if isinstance(change, dict):
                            self._save_price_change(repository, by_id, change, item)
                continue
            token_id = str(item.get("asset_id", ""))
            token = by_id.get(token_id)
            if token is None:
                continue
            if event_type == "book":
                bids = _levels(item.get("bids"))
                asks = _levels(item.get("asks"))
                self._books[token_id] = (bids, asks)
                bid = max(bids, default=None)
                ask = min(asks, default=None)
                self._save_quote(repository, token, bid, ask, None, item.get("timestamp"))
                self._save_book_metrics(repository, token, bids, asks, item.get("timestamp"))
            elif event_type == "last_trade_price":
                last = _decimal(item.get("price"))
                self._save_quote(repository, token, None, None, last, item.get("timestamp"))
                size = _decimal(item.get("size"))
                if last is not None and size is not None:
                    repository.append_market_trade(
                        token=token,
                        side=str(item.get("side", "UNKNOWN")).upper(),
                        price=last,
                        size=size,
                        fee_rate_bps=_decimal(item.get("fee_rate_bps")),
                        source_timestamp=_timestamp(item.get("timestamp")),
                    )

    def _save_price_change(
        self,
        repository: MarketRepository,
        by_id: dict[str, OutcomeToken],
        change: dict[str, Any],
        envelope: dict[str, Any],
    ) -> None:
        token = by_id.get(str(change.get("asset_id", "")))
        if token is None:
            return
        self._apply_book_change(token.token_id, change)
        self._save_quote(
            repository,
            token,
            _decimal(change.get("best_bid")),
            _decimal(change.get("best_ask")),
            None,
            envelope.get("timestamp"),
        )
        book = self._books.get(token.token_id)
        if book is not None:
            self._save_book_metrics(repository, token, book[0], book[1], envelope.get("timestamp"))

    def _apply_book_change(self, token_id: str, change: dict[str, Any]) -> None:
        book = self._books.get(token_id)
        price = _decimal(change.get("price"))
        size = _decimal(change.get("size"))
        if book is None or price is None or size is None:
            return
        levels = book[0] if str(change.get("side", "")).upper() == "BUY" else book[1]
        if size == 0:
            levels.pop(price, None)
        else:
            levels[price] = size

    def _save_book_metrics(
        self,
        repository: MarketRepository,
        token: OutcomeToken,
        bids: dict[Decimal, Decimal],
        asks: dict[Decimal, Decimal],
        raw_timestamp: object,
    ) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_book_metric.get(token.token_id, 0) < 1:
            return
        self._last_book_metric[token.token_id] = now_monotonic
        best_bid = max(bids, default=None)
        best_ask = min(asks, default=None)
        bid_cutoff = best_bid - Decimal("0.01") if best_bid is not None else None
        ask_cutoff = best_ask + Decimal("0.01") if best_ask is not None else None
        bid_near = sum(
            (
                size
                for price, size in bids.items()
                if bid_cutoff is not None and price >= bid_cutoff
            ),
            Decimal("0"),
        )
        ask_near = sum(
            (
                size
                for price, size in asks.items()
                if ask_cutoff is not None and price <= ask_cutoff
            ),
            Decimal("0"),
        )
        repository.append_book_metrics(
            token=token,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth_1pct=bid_near,
            ask_depth_1pct=ask_near,
            bid_depth_total=sum(bids.values(), Decimal("0")),
            ask_depth_total=sum(asks.values(), Decimal("0")),
            source_timestamp=_timestamp(raw_timestamp),
        )

    def _save_quote(
        self,
        repository: MarketRepository,
        token: OutcomeToken,
        bid: Decimal | None,
        ask: Decimal | None,
        last: Decimal | None,
        raw_timestamp: object,
    ) -> None:
        repository.upsert_quote(
            token,
            best_bid=bid,
            best_ask=ask,
            last_trade_price=last,
            source_timestamp=_timestamp(raw_timestamp),
        )
        now_monotonic = time.monotonic()
        if last is None and now_monotonic - self._last_tick_persist.get(token.token_id, 0) < 0.25:
            return
        self._last_tick_persist[token.token_id] = now_monotonic
        repository.append_tick(
            venue="polymarket",
            instrument=token.token_id,
            market_id=token.market_id,
            outcome=token.outcome,
            best_bid=bid,
            best_ask=ask,
            last_trade_price=last,
            source_timestamp=_timestamp(raw_timestamp),
        )
        for price_type, value in (("bid", bid), ("ask", ask), ("last", last)):
            if value is not None:
                PRICE.labels(
                    market_id=token.market_id,
                    outcome=token.outcome,
                    price_type=price_type,
                ).set(float(value))
