import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter, Gauge

from prediction_trader.adapters.polymarket import DiscoveredMarket, GammaClient, parse_events
from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository

RUNS = Counter("polymarket_discovery_runs_total", "Market discovery runs", ["profile", "result"])
MARKETS = Gauge(
    "polymarket_discovered_markets", "Markets matching the configured profile", ["profile"]
)
LAST_SUCCESS = Gauge(
    "polymarket_discovery_last_success_unixtime",
    "Unix time of the last successful discovery",
    ["profile"],
)


class MarketDiscoveryService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop
        self._logger = logging.getLogger("prediction_trader.discovery")
        self._profile_id = settings.profile.id

    def discover(
        self, client: GammaClient, *, now: datetime | None = None
    ) -> list[DiscoveredMarket]:
        """Fetch only adjacent windows in the selected game; keep partial successes."""
        profile = self._settings.profile
        current = profile.window_start(now or datetime.now(UTC))
        discovered: dict[str, DiscoveredMarket] = {}
        successful_requests = 0
        for offset in (-1, 0, 1):
            if self._stop.is_set():
                break
            window = current + timedelta(seconds=offset * profile.duration_seconds)
            slug = profile.slug_at(window)
            try:
                detail = client.get_event_by_slug(slug)
                matches = parse_events([detail], profile=profile) if detail is not None else []
                # A response for another window must not widen the subscription scope.
                for market in matches:
                    if market.market_slug == slug:
                        discovered[market.market_id] = market
                successful_requests += 1
            except Exception:
                self._logger.warning("Market discovery failed for %s", slug, exc_info=True)
        if not successful_requests and not self._stop.is_set():
            raise RuntimeError(f"All discovery requests failed for {profile.id}")
        return list(discovered.values())

    def run(self) -> None:
        client = GammaClient(self._settings.polymarket_gamma_url)
        repository = MarketRepository(self._settings.database_url)
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    discovered = self.discover(client)
                    repository.upsert(discovered)
                    MARKETS.labels(profile=self._profile_id).set(len(discovered))
                    LAST_SUCCESS.labels(profile=self._profile_id).set_to_current_time()
                    RUNS.labels(profile=self._profile_id, result="success").inc()
                    self._logger.info(
                        "Polymarket discovery stored %d matching markets for %s",
                        len(discovered),
                        self._profile_id,
                    )
                except Exception:
                    RUNS.labels(profile=self._profile_id, result="error").inc()
                    self._logger.exception("Polymarket discovery failed for %s", self._profile_id)
                elapsed = time.monotonic() - started
                wait_for = max(0, self._settings.polymarket_discovery_interval_seconds - elapsed)
                self._stop.wait(wait_for)
        finally:
            client.close()
            repository.close()
