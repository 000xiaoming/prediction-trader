import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from prometheus_client import Counter, Gauge
from websockets.sync.client import connect

from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository

_LABELS = ["symbol", "window_seconds"]
CONNECTED = Gauge(
    "polymarket_rtds_chainlink_connected", "Chainlink RTDS connection state", _LABELS
)
PRICE = Gauge("chainlink_usd_twap", "Latest Chainlink USD TWAP reference", _LABELS)
LAST_UPDATE = Gauge("chainlink_last_update_unixtime", "Latest Chainlink update time", _LABELS)
MESSAGES = Counter(
    "polymarket_rtds_chainlink_messages_total", "Chainlink RTDS price messages", _LABELS
)
MAX_SILENCE_SECONDS = 15
TWAP_TOPICS = {30: "crypto_prices_twap_thirty", 60: "crypto_prices_twap_sixty"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_TIMESTAMP_MS = 253_402_300_799_999


def _twap_topic(window_seconds: int) -> str:
    if type(window_seconds) is not int or window_seconds not in TWAP_TOPICS:
        raise ValueError("Chainlink TWAP window must be 30 or 60 seconds")
    return TWAP_TOPICS[window_seconds]


def chainlink_reference_source(window_seconds: int) -> str:
    """Keep each TWAP window separate from the historical spot-price series."""
    _twap_topic(window_seconds)
    return f"polymarket_rtds_chainlink_twap_{window_seconds}"


def _price_point(item: object, *, twap: bool = False) -> tuple[datetime, Decimal] | None:
    if not isinstance(item, dict):
        return None
    try:
        timestamp = Decimal(str(item.get("timestamp")))
        if not timestamp.is_finite() or timestamp <= 0:
            return None
        if twap:
            # Raw RTDS TWAP timestamps are observation times in epoch milliseconds.
            if timestamp != timestamp.to_integral_value():
                return None
            timestamp_ms = timestamp
            raw_price = item.get("full_accuracy_value")
            # The display-only float loses precision and must never be a fallback.
            if (
                not isinstance(raw_price, str)
                or len(raw_price) > 128
                or re.fullmatch(r"[+-]?[0-9]+", raw_price) is None
            ):
                return None
            exact = Decimal(raw_price).as_tuple()
            price = Decimal((exact.sign, exact.digits, -18))
        else:
            timestamp_ms = timestamp if timestamp > 10_000_000_000 else timestamp * 1000
            price = Decimal(str(item.get("value")))
        if timestamp_ms > _MAX_TIMESTAMP_MS or not price.is_finite() or price <= 0:
            return None
        source_time = _EPOCH + timedelta(microseconds=int(timestamp_ms * 1000))
        return source_time, price
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def parse_chainlink_message(
    message: str | bytes,
    expected_symbol: str = "btc/usd",
    *,
    expected_window_seconds: int | None = None,
) -> list[tuple[datetime, Decimal]]:
    """Parse legacy spot fixtures or, explicitly, a verified raw RTDS TWAP update."""
    expected_topic = (
        "crypto_prices_chainlink"
        if expected_window_seconds is None
        else _twap_topic(expected_window_seconds)
    )
    try:
        payload: Any = json.loads(message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("topic") != expected_topic:
        return []
    body = payload.get("payload")
    if expected_window_seconds is not None:
        if (
            payload.get("type") != "update"
            or not isinstance(body, dict)
            or body.get("symbol") != expected_symbol
            or type(body.get("window_s")) is not int
            or body.get("window_s") != expected_window_seconds
        ):
            return []
        point = _price_point(body, twap=True)
        return [point] if point is not None else []
    if isinstance(body, dict) and body.get("symbol") not in (None, expected_symbol):
        return []
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        candidates = body["data"]
    elif isinstance(body, list):
        candidates = body
    else:
        candidates = [body]
    return [
        point
        for item in candidates
        if isinstance(item, dict)
        and item.get("symbol") in (None, expected_symbol)
        and (point := _price_point(item)) is not None
    ]


class ChainlinkReferenceService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._symbol = settings.profile.chainlink_symbol
        self._reference_symbol = settings.profile.reference_symbol
        self._stop = stop
        self._logger = logging.getLogger("prediction_trader.chainlink")
        self._window_seconds = settings.polymarket_chainlink_twap_window_seconds
        self._topic = _twap_topic(self._window_seconds)
        self._source = chainlink_reference_source(self._window_seconds)
        labels = {"symbol": self._reference_symbol, "window_seconds": str(self._window_seconds)}
        self._connected = CONNECTED.labels(**labels)
        self._price = PRICE.labels(**labels)
        self._last_update = LAST_UPDATE.labels(**labels)
        self._messages = MESSAGES.labels(**labels)

    def run(self) -> None:
        repository = MarketRepository(self._settings.database_url)
        try:
            while not self._stop.is_set():
                try:
                    self._consume(repository)
                except Exception:
                    self._connected.set(0)
                    self._logger.exception("Polymarket Chainlink TWAP RTDS disconnected")
                    self._stop.wait(3)
        finally:
            self._connected.set(0)
            repository.close()

    def _consume(self, repository: MarketRepository) -> None:
        with connect(self._settings.polymarket_rtds_url, open_timeout=20) as websocket:
            websocket.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": self._topic,
                                "type": "update",
                                "filters": json.dumps(
                                    {"symbol": self._symbol},
                                    separators=(",", ":"),
                                ),
                            }
                        ],
                    }
                )
            )
            self._connected.set(1)
            self._logger.info(
                "Connected to Polymarket Chainlink %s %ss TWAP RTDS",
                self._reference_symbol,
                self._window_seconds,
            )
            last_ping = time.monotonic()
            last_price = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_price > MAX_SILENCE_SECONDS:
                    raise TimeoutError(
                        f"Chainlink RTDS produced no valid {self._reference_symbol} TWAP for "
                        f"{MAX_SILENCE_SECONDS} seconds"
                    )
                if now - last_ping >= 5:
                    websocket.send("PING")
                    last_ping = now
                try:
                    message = websocket.recv(timeout=1)
                except TimeoutError:
                    continue
                if not isinstance(message, (str, bytes)):
                    continue
                points = parse_chainlink_message(
                    message,
                    self._symbol,
                    expected_window_seconds=self._window_seconds,
                )
                if points:
                    last_price = time.monotonic()
                for source_time, price in points:
                    repository.append_reference_price(
                        source=self._source,
                        symbol=self._reference_symbol,
                        price=price,
                        source_timestamp=source_time,
                    )
                    self._price.set(float(price))
                    self._last_update.set(source_time.timestamp())
                    self._messages.inc()
