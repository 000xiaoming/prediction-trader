"""Plan shared collectors and supervise the required runtime workers."""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from prediction_trader.config import Settings
from prediction_trader.services.binance_reference import BinanceReferenceService
from prediction_trader.services.candle_aggregator import CandleAggregatorService
from prediction_trader.services.chainlink_reference import ChainlinkReferenceService
from prediction_trader.services.indicator_server import IndicatorServerService
from prediction_trader.services.market_discovery import MarketDiscoveryService
from prediction_trader.services.market_stream import MarketStreamService
from prediction_trader.services.profile_backtests import ProfileBacktestService
from prediction_trader.services.settlement_sync import SettlementSyncService

WorkerKind = Literal[
    "discovery", "market", "binance", "chainlink", "candles", "indicators", "settlement",
    "backtests",
]


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    name: str
    kind: WorkerKind
    settings: Settings


def collector_plan(settings: Settings) -> tuple[WorkerSpec, ...]:
    """One market socket, profile discovery, and shared reference feeds by symbol."""
    plan: list[WorkerSpec] = []
    binance_symbols: set[str] = set()
    chainlink_sources: set[tuple[str, int]] = set()
    for profile in settings.enabled_profiles:
        effective = settings.for_profile(profile.id)
        if settings.polymarket_discovery_enabled:
            plan.append(WorkerSpec(f"discovery:{profile.id}", "discovery", effective))
        if settings.binance_reference_enabled and profile.binance_symbol not in binance_symbols:
            binance_symbols.add(profile.binance_symbol)
            plan.append(WorkerSpec(f"binance:{profile.binance_symbol}", "binance", effective))
        chainlink_key = (
            profile.chainlink_symbol,
            effective.polymarket_chainlink_twap_window_seconds,
        )
        if settings.polymarket_rtds_enabled and chainlink_key not in chainlink_sources:
            chainlink_sources.add(chainlink_key)
            name = f"chainlink:{chainlink_key[0]}:{chainlink_key[1]}"
            plan.append(WorkerSpec(name, "chainlink", effective))
    if settings.polymarket_websocket_enabled:
        plan.append(WorkerSpec("polymarket-market-stream", "market", settings))
    plan.extend(
        (
            WorkerSpec("candle-aggregator", "candles", settings),
            WorkerSpec("indicator-server", "indicators", settings),
        )
    )
    if settings.settlement_sync_enabled:
        plan.append(WorkerSpec("polymarket-settlement-sync", "settlement", settings))
    if settings.profile_backtests_enabled:
        plan.append(WorkerSpec("profile-backtests", "backtests", settings))
    return tuple(plan)


class _Service(Protocol):
    def run(self) -> None: ...


_FACTORIES: Mapping[WorkerKind, Callable[[Settings, threading.Event], _Service]] = {
    "discovery": MarketDiscoveryService,
    "market": MarketStreamService,
    "binance": BinanceReferenceService,
    "chainlink": ChainlinkReferenceService,
    "candles": CandleAggregatorService,
    "indicators": IndicatorServerService,
    "settlement": SettlementSyncService,
    "backtests": ProfileBacktestService,
}


def build_workers(settings: Settings, stop: threading.Event) -> dict[str, Callable[[], None]]:
    return {
        spec.name: _FACTORIES[spec.kind](spec.settings, stop).run
        for spec in collector_plan(settings)
    }


def supervise_workers(
    workers: Mapping[str, Callable[[], None]],
    stop: threading.Event,
    *,
    ready: Callable[[bool], None],
    worker_alive: Callable[[str, bool], None],
    join_timeout: float = 25,
) -> None:
    """Withdraw readiness and exit if any required worker ends unexpectedly."""
    if not workers:
        ready(False)
        raise ValueError("At least one required worker must be configured")
    logger = logging.getLogger("prediction_trader.runtime")
    threads: list[threading.Thread] = []
    failures: list[str] = []
    lock = threading.Lock()

    def run(name: str, target: Callable[[], None]) -> None:
        worker_alive(name, True)
        try:
            target()
        except Exception:
            logger.exception("Required worker failed: %s", name)
        finally:
            worker_alive(name, False)
            with lock:
                if not stop.is_set():
                    failures.append(name)
                    ready(False)
                    stop.set()

    ready(False)
    try:
        for name, target in workers.items():
            if stop.is_set():
                break
            worker = threading.Thread(target=run, args=(name, target), name=name, daemon=True)
            worker.start()
            threads.append(worker)
        with lock:
            ready(bool(threads) and not stop.is_set() and all(t.is_alive() for t in threads))
        stop.wait()
    finally:
        ready(False)
        stop.set()
        deadline = time.monotonic() + join_timeout
        for worker in threads:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        unjoined = [worker.name for worker in threads if worker.is_alive()]
        if unjoined:
            logger.warning("Workers exceeded shutdown timeout: %s", ", ".join(unjoined))
    if failures:
        raise RuntimeError(f"Required workers exited unexpectedly: {', '.join(failures)}")
