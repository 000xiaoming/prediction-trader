import json
import logging
import math
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from prometheus_client import Counter, Gauge
from websockets.sync.client import connect

from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository

CONNECTED = Gauge(
    "binance_websocket_connected", "Whether the Binance market stream is connected", ["symbol"]
)
MESSAGES = Counter("binance_websocket_messages_total", "Binance market messages", ["symbol"])
PRICE = Gauge("binance_price", "Binance reference price", ["symbol", "price_type"])
LAST_MESSAGE = Gauge(
    "binance_websocket_last_message_unixtime", "Unix time of the last Binance message", ["symbol"]
)


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"Invalid Binance decimal: {value!r}") from error
    if not parsed.is_finite() or parsed <= 0 or not math.isfinite(float(parsed)):
        raise ValueError("Binance prices must be finite and positive")
    return parsed


def parse_binance_book_ticker(
    payload: object, *, expected_symbol: str
) -> tuple[Decimal, Decimal, datetime | None] | None:
    """Keep unverified symbols and malformed/crossed books out of reference prices."""
    if not isinstance(payload, dict) or payload.get("s") != expected_symbol.upper():
        return None
    try:
        bid = _decimal(payload.get("b"))
        ask = _decimal(payload.get("a"))
        if bid > ask:
            return None
        event_time = payload.get("E")
        source_timestamp = None
        if event_time is not None:
            if type(event_time) is not int or event_time <= 0:
                return None
            source_timestamp = datetime.fromtimestamp(event_time / 1000, UTC)
        return bid, ask, source_timestamp
    except (ValueError, OverflowError, OSError):
        return None


class BinanceReferenceService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop
        self._logger = logging.getLogger("prediction_trader.binance")
        self._symbol = settings.profile.binance_symbol
        self._connected = CONNECTED.labels(symbol=self._symbol.upper())
        self._messages = MESSAGES.labels(symbol=self._symbol.upper())
        self._last_message = LAST_MESSAGE.labels(symbol=self._symbol.upper())

    def run(self) -> None:
        repository = MarketRepository(self._settings.database_url)
        symbol = self._symbol
        url = f"{self._settings.binance_market_ws_base_url.rstrip('/')}/{symbol}@bookTicker"
        last_persisted = 0.0
        try:
            while not self._stop.is_set():
                try:
                    with connect(url, open_timeout=20) as websocket:
                        self._connected.set(1)
                        self._logger.info("Connected to Binance %s book ticker", symbol.upper())
                        while not self._stop.is_set():
                            message = websocket.recv(timeout=2)
                            try:
                                payload = json.loads(message)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            parsed = parse_binance_book_ticker(payload, expected_symbol=symbol)
                            if parsed is None:
                                continue
                            bid, ask, source_timestamp = parsed
                            now = time.monotonic()
                            if (
                                now - last_persisted
                                >= self._settings.binance_persist_interval_seconds
                            ):
                                repository.append_tick(
                                    venue="binance",
                                    instrument=symbol.upper(),
                                    best_bid=bid,
                                    best_ask=ask,
                                    source_timestamp=source_timestamp,
                                )
                                last_persisted = now
                            for price_type, value in (
                                ("bid", bid), ("ask", ask), ("mid", (bid + ask) / 2)
                            ):
                                PRICE.labels(symbol=symbol.upper(), price_type=price_type).set(
                                    float(value)
                                )
                            self._messages.inc()
                            self._last_message.set_to_current_time()
                except TimeoutError:
                    continue
                except Exception:
                    self._connected.set(0)
                    self._logger.exception("Binance market stream disconnected")
                    self._stop.wait(3)
        finally:
            self._connected.set(0)
            repository.close()
