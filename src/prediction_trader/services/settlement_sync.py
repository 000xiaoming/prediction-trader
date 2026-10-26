import logging
import threading

from prometheus_client import Counter, Gauge

from prediction_trader.adapters.polymarket import GammaClient
from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository
from prediction_trader.settlement import SettlementSynchronizer

RUNS = Counter("polymarket_settlement_sync_runs_total", "Settlement sync runs", ["result"])
RESOLVED = Gauge("polymarket_settlement_resolved_markets", "Resolved replay markets")
SETTLED_FILLS = Counter(
    "polymarket_settlement_fills_total", "Replay fills newly assigned a final outcome"
)


class SettlementSyncService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop
        self._logger = logging.getLogger("prediction_trader.settlement")

    def run(self) -> None:
        client = GammaClient(self._settings.polymarket_gamma_url)
        repository = MarketRepository(self._settings.database_url)
        synchronizer = SettlementSynchronizer(repository, client)
        try:
            while not self._stop.is_set():
                try:
                    result = synchronizer.sync()
                    RESOLVED.set(result.resolved_markets)
                    SETTLED_FILLS.inc(result.newly_settled_fills)
                    RUNS.labels(result="success").inc()
                    if result.newly_settled_fills:
                        self._logger.info(
                            "Settlement sync resolved %d markets and %d fills",
                            result.resolved_markets,
                            result.newly_settled_fills,
                        )
                except Exception:
                    RUNS.labels(result="error").inc()
                    self._logger.exception("Polymarket settlement sync failed")
                self._stop.wait(self._settings.settlement_sync_interval_seconds)
        finally:
            client.close()
            repository.close()
