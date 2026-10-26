import logging
import threading

from prometheus_client import Counter, Gauge

from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository

RUNS = Counter("candle_aggregation_runs_total", "Candle aggregation runs", ["result"])
ROWS = Gauge("candle_aggregation_rows", "Rows affected by the latest candle aggregation")


class CandleAggregatorService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop
        self._logger = logging.getLogger("prediction_trader.candles")

    def run(self) -> None:
        repository = MarketRepository(self._settings.database_url)
        try:
            while not self._stop.is_set():
                try:
                    rows = repository.refresh_one_minute_candles()
                    ROWS.set(rows)
                    RUNS.labels(result="success").inc()
                except Exception:
                    RUNS.labels(result="error").inc()
                    self._logger.exception("One-minute candle aggregation failed")
                self._stop.wait(self._settings.candle_refresh_seconds)
        finally:
            repository.close()
