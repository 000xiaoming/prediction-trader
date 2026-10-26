import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch

from prediction_trader.config import Settings
from prediction_trader.services.chainlink_reference import (
    ChainlinkReferenceService,
    chainlink_reference_source,
    parse_chainlink_message,
)


class ChainlinkReferenceTests(TestCase):
    def test_parses_live_update(self) -> None:
        points = parse_chainlink_message(
            json.dumps(
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "update",
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": 1_787_495_700_000,
                        "value": 115_123.45,
                    },
                }
            )
        )

        self.assertEqual(points[0][1], Decimal("115123.45"))

    def test_ignores_other_topics(self) -> None:
        self.assertEqual(parse_chainlink_message('{"topic":"crypto_prices","payload":{}}'), [])

    def test_ignores_other_chainlink_symbols(self) -> None:
        self.assertEqual(
            parse_chainlink_message(
                '{"topic":"crypto_prices_chainlink","payload":'
                '{"symbol":"eth/usd","timestamp":1787495700000,"value":2000}}'
            ),
            [],
        )

    def test_ignores_text_pong(self) -> None:
        self.assertEqual(parse_chainlink_message("pong"), [])


class ChainlinkTwapTests(TestCase):
    @staticmethod
    def message(window: int = 60, **overrides: object) -> str:
        payload: dict[str, object] = {
            "symbol": "btc/usd",
            "timestamp": 1_787_495_700_123,
            "window_s": window,
            "value": 1.0,
            "full_accuracy_value": "115123451234567890123456",
        }
        payload.update(overrides)
        return json.dumps(
            {
                "topic": "crypto_prices_twap_sixty"
                if window == 60
                else "crypto_prices_twap_thirty",
                "type": "update",
                "timestamp": 1_787_495_999_999,
                "payload": payload,
            }
        )

    def test_twap_uses_exact_e18_and_observation_timestamp(self) -> None:
        points = parse_chainlink_message(self.message(), expected_window_seconds=60)
        self.assertEqual(
            points,
            [
                (
                    datetime(2026, 8, 23, 14, 35, 0, 123000, tzinfo=UTC),
                    Decimal("115123.451234567890123456"),
                )
            ],
        )

    def test_twap_keeps_precision_beyond_decimal_context(self) -> None:
        exact = "123456789012345678901234567890123456789"
        points = parse_chainlink_message(
            self.message(full_accuracy_value=exact), expected_window_seconds=60
        )
        self.assertEqual(points[0][1], Decimal("123456789012345678901.234567890123456789"))

    def test_supports_both_windows_in_separate_sources(self) -> None:
        for window in (30, 60):
            with self.subTest(window=window):
                self.assertEqual(
                    chainlink_reference_source(window),
                    f"polymarket_rtds_chainlink_twap_{window}",
                )
                self.assertEqual(
                    len(
                        parse_chainlink_message(
                            self.message(window), expected_window_seconds=window
                        )
                    ),
                    1,
                )

    def test_rejects_unknown_window(self) -> None:
        for window in (0, 15, 300, True, 60.0):
            with self.subTest(window=window), self.assertRaises(ValueError):
                chainlink_reference_source(window)

    def test_rejects_spot_and_other_twap_topic(self) -> None:
        for topic in ("crypto_prices_chainlink", "crypto_prices_twap_thirty"):
            with self.subTest(topic=topic):
                message = json.loads(self.message())
                message["topic"] = topic
                self.assertEqual(
                    parse_chainlink_message(json.dumps(message), expected_window_seconds=60),
                    [],
                )

    def test_rejects_missing_or_incorrect_symbol_and_window(self) -> None:
        for field, value in (
            ("symbol", None),
            ("symbol", "eth/usd"),
            ("symbol", "BTC/USD"),
            ("window_s", None),
            ("window_s", 30),
            ("window_s", "60"),
            ("window_s", 60.0),
            ("window_s", True),
        ):
            with self.subTest(field=field, value=value):
                self.assertEqual(
                    parse_chainlink_message(
                        self.message(**{field: value}), expected_window_seconds=60
                    ),
                    [],
                )

    def test_requires_exact_positive_integer_price(self) -> None:
        for value in (
            None,
            11512345,
            1.2,
            "",
            "NaN",
            "Infinity",
            "-1",
            "0",
            "1.1",
            "1e18",
            " 1000",
            "9" * 129,
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_chainlink_message(
                        self.message(full_accuracy_value=value), expected_window_seconds=60
                    ),
                    [],
                )

    def test_rejects_invalid_or_out_of_range_timestamp(self) -> None:
        for value in (None, "NaN", "Infinity", -1, 0, 1.5, "1e1000", 253_402_300_800_000):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_chainlink_message(
                        self.message(timestamp=value), expected_window_seconds=60
                    ),
                    [],
                )

    def test_rejects_malformed_frames_and_non_update(self) -> None:
        for message in ("PING", "PONG", "null", "[]", "{}", b"\xff"):
            with self.subTest(message=message):
                self.assertEqual(parse_chainlink_message(message, expected_window_seconds=60), [])
        message = json.loads(self.message())
        message["type"] = "snapshot"
        self.assertEqual(
            parse_chainlink_message(json.dumps(message), expected_window_seconds=60), []
        )

    def test_service_uses_selected_eth_profile(self) -> None:
        settings = Settings(_env_file=None, trading_profile="polymarket_eth_5m")
        service = ChainlinkReferenceService(settings, threading.Event())
        self.assertEqual(service._symbol, "eth/usd")
        self.assertEqual(service._reference_symbol, "ETH/USD")

    def test_service_subscribes_to_twap_and_writes_isolated_source(self) -> None:
        stop = threading.Event()
        settings = Settings(
            _env_file=None,
            polymarket_rtds_url="wss://unused.invalid",
            polymarket_chainlink_symbol="btc/usd",
            polymarket_chainlink_twap_window_seconds=60,
        )
        service = ChainlinkReferenceService(settings, stop)
        repository = Mock()
        websocket = Mock()
        calls = 0

        def recv(*, timeout: int) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.message()
            stop.set()
            return "PONG"

        websocket.recv.side_effect = recv
        with (
            patch("prediction_trader.services.chainlink_reference.connect") as connect,
            patch(
                "prediction_trader.services.chainlink_reference.time.monotonic",
                side_effect=[0, 0, 6, 6, 6],
            ),
        ):
            connect.return_value.__enter__.return_value = websocket
            service._consume(repository)
        subscription = json.loads(websocket.send.call_args_list[0].args[0])
        self.assertEqual(
            subscription["subscriptions"],
            [
                {
                    "topic": "crypto_prices_twap_sixty",
                    "type": "update",
                    "filters": '{"symbol":"btc/usd"}',
                }
            ],
        )
        websocket.send.assert_any_call("PING")
        self.assertEqual(repository.append_reference_price.call_count, 1)
        self.assertEqual(
            repository.append_reference_price.call_args.kwargs["source"],
            "polymarket_rtds_chainlink_twap_60",
        )
        self.assertEqual(
            repository.append_reference_price.call_args.kwargs["price"],
            Decimal("115123.451234567890123456"),
        )

    def test_invalid_traffic_cannot_prevent_silence_reconnect(self) -> None:
        stop = threading.Event()
        settings = Settings(
            _env_file=None,
            polymarket_rtds_url="wss://unused.invalid",
            polymarket_chainlink_symbol="btc/usd",
            polymarket_chainlink_twap_window_seconds=60,
        )
        service = ChainlinkReferenceService(settings, stop)
        repository = Mock()
        websocket = Mock()
        websocket.recv.return_value = "PONG"
        with (
            patch("prediction_trader.services.chainlink_reference.connect") as connect,
            patch(
                "prediction_trader.services.chainlink_reference.time.monotonic",
                side_effect=[0, 0, 6, 16],
            ),
        ):
            connect.return_value.__enter__.return_value = websocket
            with self.assertRaisesRegex(TimeoutError, "no valid BTC/USD TWAP"):
                service._consume(repository)
        repository.append_reference_price.assert_not_called()

    def test_eth_service_rejects_btc_and_persists_eth_with_own_metrics(self) -> None:
        stop = threading.Event()
        settings = Settings(_env_file=None, trading_profile="polymarket_eth_5m")
        service = ChainlinkReferenceService(settings, stop)
        repository = Mock()
        websocket = Mock()
        frames = iter(
            [
                self.message(),
                self.message(symbol="eth/usd", full_accuracy_value="2000000000000000000000"),
            ]
        )

        def recv(*, timeout: int) -> str:
            try:
                return next(frames)
            except StopIteration:
                stop.set()
                return "PONG"

        websocket.recv.side_effect = recv
        with (
            patch("prediction_trader.services.chainlink_reference.connect") as connect,
            patch("prediction_trader.services.chainlink_reference.PRICE") as price_metric,
        ):
            # Construct after patching so metric children use the selected labels.
            service = ChainlinkReferenceService(settings, stop)
            connect.return_value.__enter__.return_value = websocket
            service._consume(repository)
        price_metric.labels.assert_called_once_with(symbol="ETH/USD", window_seconds="60")
        price_metric.labels.return_value.set.assert_called_once_with(2000.0)
        subscription = json.loads(websocket.send.call_args_list[0].args[0])
        self.assertEqual(subscription["subscriptions"][0]["filters"], '{"symbol":"eth/usd"}')
        repository.append_reference_price.assert_called_once()
        self.assertEqual(repository.append_reference_price.call_args.kwargs["symbol"], "ETH/USD")
        self.assertEqual(
            repository.append_reference_price.call_args.kwargs["price"], Decimal("2000")
        )
