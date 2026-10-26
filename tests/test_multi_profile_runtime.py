import threading
from collections import Counter
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from prediction_trader.config import Settings
from prediction_trader.persistence import OutcomeToken
from prediction_trader.profiles import MarketProfile
from prediction_trader.runtime import collector_plan, supervise_workers
from prediction_trader.services.market_discovery import MarketDiscoveryService
from prediction_trader.services.market_stream import MarketStreamService

BTC5 = "polymarket_btc_5m"
BTC15 = "polymarket_btc_15m"
ETH5 = "polymarket_eth_5m"


def test_enabled_profiles_default_primary_first_and_deduplicated() -> None:
    assert [p.id for p in Settings(_env_file=None).enabled_profiles] == [BTC5]
    settings = Settings(
        _env_file=None,
        trading_profile=ETH5,
        enabled_trading_profiles=f" {BTC5}, {BTC15}, {BTC5} ",
    )
    assert [p.id for p in settings.enabled_profiles] == [ETH5, BTC5, BTC15]
    assert settings.enabled_trading_profiles == f"{BTC5},{BTC15}"


@pytest.mark.parametrize("value", ["typo", f"{BTC5},", f"{BTC5},,{ETH5}"])
def test_invalid_enabled_profile_fails_preflight(value: str) -> None:
    with pytest.raises(ValidationError, match="Unknown trading profile"):
        Settings(_env_file=None, enabled_trading_profiles=value)


def test_profile_view_revalidates_and_preserves_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        enabled_trading_profiles=ETH5,
        indicator_port=9080,
        research_context_path="/research/{symbol}.json",
        polymarket_chainlink_twap_window_seconds=30,
    )
    monkeypatch.setenv("INDICATOR_PORT", "6666")
    monkeypatch.setenv("BINANCE_SYMBOL", "invalid")
    effective = settings.for_profile(ETH5)
    assert effective is not settings
    assert effective.profile.id == ETH5
    assert effective.indicator_port == 9080
    assert effective.binance_symbol == "ethusdt"
    assert effective.polymarket_chainlink_symbol == "eth/usd"
    assert effective.polymarket_series_slug_prefix == "eth-updown-5m"
    assert effective.polymarket_chainlink_twap_window_seconds == 30
    assert effective.resolved_research_context_path == "/research/eth-usd.json"
    assert effective.profile_overrides == ()
    assert {p.id for p in effective.enabled_profiles} == {BTC5, ETH5}
    assert settings.profile.id == BTC5
    assert settings.binance_symbol == "btcusdt"
    with pytest.raises(ValidationError, match="frozen"):
        effective.indicator_port = 1
    with pytest.raises(ValueError, match="not enabled"):
        settings.for_profile(BTC15)


def test_three_profiles_share_reference_collectors_and_singletons() -> None:
    plan = collector_plan(
        Settings(_env_file=None, enabled_trading_profiles=f"{BTC5},{BTC15},{ETH5}")
    )
    assert Counter(spec.kind for spec in plan) == {
        "discovery": 3,
        "market": 1,
        "binance": 2,
        "chainlink": 2,
        "candles": 1,
        "indicators": 1,
        "settlement": 1,
    }
    assert len({spec.name for spec in plan}) == len(plan)
    assert {spec.settings.profile.id for spec in plan if spec.kind == "discovery"} == {
        BTC5,
        BTC15,
        ETH5,
    }
    assert {spec.settings.binance_symbol for spec in plan if spec.kind == "binance"} == {
        "btcusdt",
        "ethusdt",
    }


def test_collector_defaults_and_disabled_sources() -> None:
    assert len(collector_plan(Settings(_env_file=None))) == 7
    plan = collector_plan(
        Settings(
            _env_file=None,
            enabled_trading_profiles=f"{BTC15},{ETH5}",
            polymarket_discovery_enabled=False,
            polymarket_websocket_enabled=False,
            binance_reference_enabled=False,
            polymarket_rtds_enabled=False,
            settlement_sync_enabled=False,
        )
    )
    assert [spec.kind for spec in plan] == ["candles", "indicators"]


def test_union_subscription_uses_only_enabled_profile_queries_and_dedupes() -> None:
    settings = Settings(_env_file=None, enabled_trading_profiles=BTC15)
    repository = Mock()
    shared = OutcomeToken("shared", "shared-market", "YES")
    tokens = {
        BTC5: [OutcomeToken("btc5-current", "btc5", "YES"), shared],
        BTC15: [OutcomeToken("btc15-next", "btc15", "NO"), shared],
        ETH5: [OutcomeToken("disabled-eth", "eth", "YES")],
    }

    def list_tokens(*, profile: MarketProfile) -> list[OutcomeToken]:
        return tokens[profile.id]

    repository.list_outcome_tokens.side_effect = list_tokens
    found = MarketStreamService(settings, threading.Event()).subscription_tokens(repository)
    assert [token.token_id for token in found] == ["btc5-current", "shared", "btc15-next"]
    assert [
        call.kwargs["profile"].id for call in repository.list_outcome_tokens.call_args_list
    ] == [
        BTC5,
        BTC15,
    ]


def test_union_subscription_rejects_conflicting_token_identity() -> None:
    repository = Mock()
    repository.list_outcome_tokens.side_effect = [
        [OutcomeToken("same", "btc5", "YES")],
        [OutcomeToken("same", "btc15", "YES")],
    ]
    service = MarketStreamService(
        Settings(_env_file=None, enabled_trading_profiles=BTC15), threading.Event()
    )
    with pytest.raises(ValueError, match="Conflicting"):
        service.subscription_tokens(repository)


def test_discovery_metrics_are_isolated_per_profile() -> None:
    with (
        patch("prediction_trader.services.market_discovery.GammaClient"),
        patch("prediction_trader.services.market_discovery.MarketRepository"),
        patch("prediction_trader.services.market_discovery.MARKETS") as markets,
        patch("prediction_trader.services.market_discovery.LAST_SUCCESS") as success,
        patch("prediction_trader.services.market_discovery.RUNS") as runs,
    ):
        for profile_id in (BTC5, BTC15, ETH5):
            stop = threading.Event()
            service = MarketDiscoveryService(
                Settings(_env_file=None, trading_profile=profile_id), stop
            )

            def discover(_client: object, current_stop: threading.Event = stop) -> list[object]:
                current_stop.set()
                return []

            with patch.object(service, "discover", side_effect=discover):
                service.run()
    expected = {BTC5, BTC15, ETH5}
    assert {call.kwargs["profile"] for call in markets.labels.call_args_list} == expected
    assert {call.kwargs["profile"] for call in success.labels.call_args_list} == expected
    assert {call.kwargs["profile"] for call in runs.labels.call_args_list} == expected


@pytest.mark.parametrize("raises", [False, True])
def test_supervisor_withdraws_readiness_when_required_worker_exits(raises: bool) -> None:
    stop = threading.Event()
    exit_worker = threading.Event()
    readiness: list[bool] = []
    alive = Mock()

    def worker() -> None:
        assert exit_worker.wait(timeout=2)
        if raises:
            raise RuntimeError("collector crashed")

    def ready(value: bool) -> None:
        readiness.append(value)
        if value:
            exit_worker.set()

    with pytest.raises(RuntimeError, match="collector"):
        supervise_workers(
            {"collector": worker, "other": lambda: stop.wait(timeout=2)},
            stop,
            ready=ready,
            worker_alive=alive,
            join_timeout=2,
        )
    assert True in readiness
    assert readiness[-1] is False
    assert stop.is_set()
    assert ("collector", False) in [call.args for call in alive.call_args_list]
    assert ("other", False) in [call.args for call in alive.call_args_list]


def test_supervisor_graceful_stop_joins_all_workers() -> None:
    stop = threading.Event()
    finished = threading.Event()
    readiness: list[bool] = []

    def worker() -> None:
        stop.wait(timeout=2)
        finished.set()

    def ready(value: bool) -> None:
        readiness.append(value)
        if value:
            stop.set()

    supervise_workers({"collector": worker}, stop, ready=ready, worker_alive=Mock(), join_timeout=2)
    assert finished.is_set()
    assert readiness == [False, True, False]


def test_supervisor_withdraws_readiness_when_worker_returns_during_startup() -> None:
    readiness: list[bool] = []
    with pytest.raises(RuntimeError, match="immediate-exit"):
        supervise_workers(
            {"immediate-exit": lambda: None},
            threading.Event(),
            ready=readiness.append,
            worker_alive=Mock(),
            join_timeout=2,
        )
    assert readiness[-1] is False


def test_discovery_stop_avoids_additional_remote_requests() -> None:
    stop = threading.Event()
    client = Mock()

    def event_by_slug(_slug: str) -> None:
        stop.set()

    client.get_event_by_slug.side_effect = event_by_slug
    assert MarketDiscoveryService(Settings(_env_file=None), stop).discover(client) == []
    assert client.get_event_by_slug.call_count == 1
