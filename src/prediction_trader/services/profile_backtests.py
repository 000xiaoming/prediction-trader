"""Periodically publish bounded, independent reports for enabled profiles."""

import logging
import threading
from datetime import UTC, datetime, timedelta

from prediction_trader.backtesting.engine import BacktestParameters
from prediction_trader.backtesting.repository import BacktestRepository
from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository


class ProfileBacktestService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop

    def run(self) -> None:
        repository = MarketRepository(self._settings.database_url)
        reports = BacktestRepository(repository)
        logger = logging.getLogger("prediction_trader.backtests")
        try:
            while not self._stop.is_set():
                for profile in self._settings.enabled_profiles:
                    if self._stop.is_set():
                        break
                    now = datetime.now(UTC)
                    try:
                        reports.run(profile, BacktestParameters(
                            start=now-timedelta(hours=self._settings.profile_backtest_lookback_hours),
                            end=now, as_of=now,
                            twap_window_seconds=self._settings.polymarket_chainlink_twap_window_seconds,
                        ))
                        logger.info("Published profile report: %s", profile.id)
                    except Exception:
                        logger.exception("Profile report failed: %s", profile.id)
                self._stop.wait(self._settings.profile_backtest_interval_seconds)
        finally:
            repository.close()
