import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from prediction_trader.config import Settings
from prediction_trader.profiles import MarketProfile, get_profile
from prediction_trader.services.binance_reference import (
    BinanceReferenceService,
    parse_binance_book_ticker,
)
from prediction_trader.services.market_discovery import MarketDiscoveryService
from prediction_trader.services.market_stream import MarketStreamService

NOW = datetime(2026, 9, 19, 12, 7, tzinfo=UTC)


def event(profile: MarketProfile, start: datetime) -> dict[str, object]:
    slug = profile.slug_at(start)
    return {
        "id": f"event-{slug}",
        "slug": slug,
        "title": profile.question_prefix,
        "markets": [
            {
                "id": f"market-{slug}",
                "slug": slug,
                "question": profile.question_prefix,
                "outcomes": '["Down", "Up"]',
                "clobTokenIds": '["down-token", "up-token"]',
                "endDate": (start + timedelta(seconds=profile.duration_seconds)).isoformat(),
            }
        ],
    }


@pytest.mark.parametrize(
    "profile_id", ["polymarket_btc_5m", "polymarket_btc_15m", "polymarket_eth_5m"]
)
def test_discovery_fetches_only_selected_adjacent_windows(profile_id: str) -> None:
    settings = Settings(_env_file=None, trading_profile=profile_id)
    profile = settings.profile
    current = profile.window_start(NOW)
    starts = [current + timedelta(seconds=i * profile.duration_seconds) for i in (-1, 0, 1)]
    client = Mock()
    client.get_event_by_slug.side_effect = [event(profile, start) for start in starts]

    found = MarketDiscoveryService(settings, threading.Event()).discover(client, now=NOW)

    assert [call.args[0] for call in client.get_event_by_slug.call_args_list] == [
        profile.slug_at(start) for start in starts
    ]
    assert len(found) == 3
    assert all(market.yes_token_id == "up-token" for market in found)
    assert all(market.no_token_id == "down-token" for market in found)
    client.list_active_events.assert_not_called()
    client.search_events.assert_not_called()


def test_discovery_keeps_successes_after_a_window_request_fails() -> None:
    settings = Settings(_env_file=None)
    profile = settings.profile
    current = profile.window_start(NOW)
    client = Mock()
    client.get_event_by_slug.side_effect = [
        RuntimeError("upstream unavailable"),
        event(profile, current),
        None,
    ]
    found = MarketDiscoveryService(settings, threading.Event()).discover(client, now=NOW)
    assert [market.market_slug for market in found] == [profile.slug_at(current)]
    assert client.get_event_by_slug.call_count == 3


def test_discovery_all_failures_are_not_reported_as_success() -> None:
    client = Mock()
    client.get_event_by_slug.side_effect = RuntimeError("upstream unavailable")
    service = MarketDiscoveryService(Settings(_env_file=None), threading.Event())
    with pytest.raises(RuntimeError, match="All discovery requests failed"):
        service.discover(client, now=NOW)
    assert client.get_event_by_slug.call_count == 3


def test_discovery_rejects_other_assets_durations_and_unrequested_windows() -> None:
    settings = Settings(_env_file=None)
    client = Mock()
    client.get_event_by_slug.side_effect = [
        event(get_profile("polymarket_eth_5m"), settings.profile.window_start(NOW)),
        event(get_profile("polymarket_btc_15m"), NOW.replace(minute=0)),
        event(settings.profile, settings.profile.window_start(NOW) + timedelta(days=1)),
    ]
    assert MarketDiscoveryService(settings, threading.Event()).discover(client, now=NOW) == []


@pytest.mark.parametrize("symbol", ["btcusdt", "ethusdt"])
def test_binance_parser_checks_selected_symbol(symbol: str) -> None:
    payload = {"s": symbol.upper(), "b": "2000.001", "a": "2000.002"}
    assert parse_binance_book_ticker(payload, expected_symbol=symbol) == (
        Decimal("2000.001"), Decimal("2000.002"), None
    )
    payload["s"] = "OTHER"
    assert parse_binance_book_ticker(payload, expected_symbol=symbol) is None
    del payload["s"]
    assert parse_binance_book_ticker(payload, expected_symbol=symbol) is None


@pytest.mark.parametrize(
    "value", [None, "NaN", "sNaN", "Infinity", "-Infinity", "0", "-1", "1e999"]
)
@pytest.mark.parametrize("field", ["a", "b"])
def test_binance_parser_rejects_invalid_prices(field: str, value: object) -> None:
    payload = {"s": "ETHUSDT", "b": "2000", "a": "2001", field: value}
    assert parse_binance_book_ticker(payload, expected_symbol="ethusdt") is None


def test_binance_parser_rejects_crossed_book_and_bad_timestamps() -> None:
    payload = {"s": "ETHUSDT", "b": "2001", "a": "2000"}
    assert parse_binance_book_ticker(payload, expected_symbol="ethusdt") is None
    for timestamp in (True, -1, 0, "1789819620000", 10**100):
        assert parse_binance_book_ticker(
            {"s": "ETHUSDT", "b": "2000", "a": "2001", "E": timestamp},
            expected_symbol="ethusdt",
        ) is None


def test_binance_eth_service_persists_only_eth_and_uses_symbol_metrics() -> None:
    stop = threading.Event()
    websocket = Mock()
    frames = iter(
        [
            {"s": "BTCUSDT", "b": "60000", "a": "60001"},
            {"s": "ETHUSDT", "b": "2000", "a": "2001"},
        ]
    )

    def recv(*, timeout: int) -> str:
        try:
            return json.dumps(next(frames))
        except StopIteration:
            stop.set()
            return "{}"

    websocket.recv.side_effect = recv
    with (
        patch("prediction_trader.services.binance_reference.connect") as connect,
        patch("prediction_trader.services.binance_reference.MarketRepository") as repository_type,
        patch("prediction_trader.services.binance_reference.PRICE") as price,
    ):
        connect.return_value.__enter__.return_value = websocket
        service = BinanceReferenceService(
            Settings(_env_file=None, trading_profile="polymarket_eth_5m"), stop
        )
        service.run()
    assert connect.call_args.args[0].endswith("/ethusdt@bookTicker")
    repository_type.return_value.append_tick.assert_called_once()
    assert repository_type.return_value.append_tick.call_args.kwargs["instrument"] == "ETHUSDT"
    assert {call.kwargs["symbol"] for call in price.labels.call_args_list} == {"ETHUSDT"}


def test_market_stream_requests_only_active_profile_tokens() -> None:
    stop = threading.Event()
    settings = Settings(_env_file=None, trading_profile="polymarket_eth_5m")
    service = MarketStreamService(settings, stop)
    with patch("prediction_trader.services.market_stream.MarketRepository") as repository_type:
        repository = repository_type.return_value

        def list_tokens(*, profile: MarketProfile) -> list[object]:
            assert profile == settings.profile
            stop.set()
            return []

        repository.list_outcome_tokens.side_effect = list_tokens
        service.run()
    repository.list_outcome_tokens.assert_called_once_with(profile=settings.profile)
