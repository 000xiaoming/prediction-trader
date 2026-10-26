import logging
import signal
import threading

from prometheus_client import Gauge, start_http_server

from prediction_trader.backtesting.repository import BacktestRepository
from prediction_trader.config import Settings, TradingMode
from prediction_trader.persistence import MarketRepository
from prediction_trader.runtime import build_workers, supervise_workers
from prediction_trader.strategies.indicator_models import get_indicator_model

READY = Gauge("prediction_trader_ready", "Whether the trader process is ready")
MODE = Gauge("prediction_trader_mode", "Trading mode: paper=0, live=1")
PROFILE = Gauge(
    "prediction_trader_profile_info",
    "Enabled market profiles",
    ["profile", "asset", "duration_seconds"],
)
WORKER_ALIVE = Gauge(
    "prediction_trader_worker_alive", "Whether a required runtime worker is alive", ["worker"]
)


def main() -> None:
    settings = Settings()
    for profile in settings.enabled_profiles:
        get_indicator_model(profile.strategy_id)
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger("prediction_trader")
    logger.info("Active trading profile: %s (%s)", settings.profile.id, settings.profile.label)
    logger.info("Enabled trading profiles: %s", ", ".join(p.id for p in settings.enabled_profiles))
    if settings.profile_overrides:
        logger.warning(
            "TRADING_PROFILE overrides legacy market settings: %s",
            ", ".join(settings.profile_overrides),
        )

    if settings.trading_mode is TradingMode.LIVE:
        raise RuntimeError("Live trading is intentionally unavailable in milestone M0")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    start_http_server(settings.metrics_port, addr=settings.metrics_host)
    repository = MarketRepository(settings.database_url)
    repository.create_schema()
    BacktestRepository(repository).create_schema()
    repository.close()
    MODE.set(0)
    for profile in settings.enabled_profiles:
        PROFILE.labels(
            profile=profile.id,
            asset=profile.asset,
            duration_seconds=str(profile.duration_seconds),
        ).set(1)
    workers = build_workers(settings, stop)
    logger.info("prediction-trader started in paper mode")
    try:
        supervise_workers(
            workers,
            stop,
            ready=lambda value: READY.set(int(value)),
            worker_alive=lambda name, value: WORKER_ALIVE.labels(worker=name).set(int(value)),
        )
    finally:
        READY.set(0)
        logger.info("prediction-trader stopped")


if __name__ == "__main__":
    main()
