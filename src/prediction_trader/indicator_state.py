"""Live dashboard snapshots: bounded reads, one database snapshot, pure analysis."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository
from prediction_trader.reference_analysis import ReferencePoint
from prediction_trader.research.context import FileResearchContextProvider
from prediction_trader.research.producer import ProducerResearchContextProvider
from prediction_trader.strategies.indicator_models import get_indicator_model


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _value(value: object) -> str | None:
    return str(value) if value is not None else None


class IndicatorStateReader:
    def __init__(self, repository: MarketRepository, *, settings: Settings | None = None) -> None:
        self._repository = repository
        self._settings = settings or Settings()
        self._profile = self._settings.profile
        self._model = get_indicator_model(self._profile.strategy_id)
        self._research = FileResearchContextProvider(
            self._settings.resolved_research_context_path,
            expected_symbol=self._profile.research_symbol,
        )
        self._producer_research = ProducerResearchContextProvider(
            self._settings.tradingagents_research_dir,
            expected_symbol=self._profile.research_symbol,
        )

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        # Injected time supports testing, not replay: metadata/quotes are latest-only.
        now = now or datetime.now(UTC)
        profile = self._profile
        start_time = profile.window_start(now)
        end_time = start_time + timedelta(seconds=profile.duration_seconds)
        window = self._settings.polymarket_chainlink_twap_window_seconds
        research = self._research.read(now)
        if research["status"] != "available" and self._settings.tradingagents_research_dir:
            research = self._producer_research.read(now)
        with self._repository.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            market = (
                connection.execute(
                    text("""
                SELECT id, question, end_date, yes_token_id, no_token_id, raw
                FROM markets WHERE slug = :slug AND end_date = :end_time LIMIT 1
            """),
                    {"slug": profile.slug_at(now), "end_time": end_time},
                )
                .mappings()
                .first()
            )
            if market is None:
                return {
                    "now": now.isoformat(),
                    "status": "waiting_for_market",
                    "profile": profile.as_dict(),
                    "research": research,
                }
            next_market = (
                connection.execute(
                    text("""
                SELECT id, question, end_date FROM markets
                WHERE slug = :slug AND end_date = :end_time LIMIT 1
            """),
                    {
                        "slug": profile.slug_at(end_time),
                        "end_time": end_time + timedelta(seconds=profile.duration_seconds),
                    },
                )
                .mappings()
                .first()
            )
            prices = connection.execute(
                text("""
                SELECT source_timestamp, observed_at, price FROM reference_prices
                WHERE source = :source AND symbol = :symbol
                  AND source_timestamp >= :lookback AND source_timestamp <= :now
                  AND observed_at <= :now
                ORDER BY source_timestamp, observed_at, id
            """),
                {
                    "source": f"polymarket_rtds_chainlink_twap_{window}",
                    "symbol": profile.reference_symbol,
                    "lookback": min(start_time, now - timedelta(seconds=60)),
                    "now": now,
                },
            ).all()
            quotes = (
                connection.execute(
                    text("""
                SELECT token_id, outcome, best_bid, best_ask, last_trade_price, updated_at
                FROM market_quotes WHERE market_id = :market_id
                  AND token_id IN (:yes_token, :no_token) AND updated_at <= :now
            """),
                    {
                        "market_id": market["id"],
                        "yes_token": market["yes_token_id"],
                        "no_token": market["no_token_id"],
                        "now": now,
                    },
                )
                .mappings()
                .all()
            )
            microstructure = self._microstructure(
                connection, str(market["yes_token_id"]), str(market["no_token_id"]), now
            )

        analysis = self._model.analyze(
            [ReferencePoint(row[0], row[1], Decimal(row[2])) for row in prices],
            start=start_time,
            end=end_time,
            now=now,
            max_age_seconds=self._settings.indicator_max_reference_age_seconds,
            opening_tolerance_seconds=self._settings.indicator_opening_tolerance_seconds,
        )
        estimate = analysis.estimate
        raw = market["raw"] if isinstance(market["raw"], dict) else {}
        crypto_config = raw.get("cryptoMarketConfig") or {}
        contract_window = (
            crypto_config.get("twapLookbackSeconds") if isinstance(crypto_config, dict) else None
        )
        issues = list(analysis.issues)
        if contract_window is not None and str(contract_window) != str(window):
            issues.append("contract_twap_window_mismatch")
            estimate = None
        fee_schedule = raw.get("feeSchedule")
        fee_schedule = fee_schedule if isinstance(fee_schedule, dict) else {}
        fee_rate = fee_schedule.get("rate", 0.07)
        current, opening = analysis.current, analysis.opening
        outcomes = []
        for quote in quotes:
            micro = microstructure[str(quote["token_id"])]
            quote_age = (now - quote["updated_at"]).total_seconds()
            book_age = micro["book_age_seconds"]
            max_age = self._settings.indicator_max_quote_age_seconds
            fresh = quote_age <= max_age and book_age is not None and book_age <= max_age
            outcomes.append(
                {
                    "label": "UP" if quote["token_id"] == market["yes_token_id"] else "DOWN",
                    "token_id": quote["token_id"],
                    "bid": _value(quote["best_bid"]),
                    "ask": _value(quote["best_ask"]),
                    "last": _value(quote["last_trade_price"]),
                    "updated_at": _iso(quote["updated_at"]),
                    "quote_age_seconds": quote_age,
                    "quote_status": "fresh" if fresh else "stale_or_unverified",
                    "max_age_seconds": max_age,
                    **micro,
                }
            )
        if len(outcomes) != 2 or any(o["quote_status"] != "fresh" for o in outcomes):
            # Quote freshness does not alter the independent reference-price model.
            issues.append("quotes_stale_or_missing")
        return {
            "now": now.isoformat(),
            "status": "ok",
            "profile": profile.as_dict(),
            "market": {
                "id": market["id"],
                "question": market["question"],
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
                "remaining_seconds": max(0, int((end_time - now).total_seconds())),
                "twap_lookback_seconds": contract_window,
                "fee_rate": str(fee_rate),
                "fee_rate_source": "market_metadata" if "rate" in fee_schedule else "fallback",
            },
            "next_market": (
                {
                    "id": next_market["id"],
                    "question": next_market["question"],
                    "start": end_time.isoformat(),
                    "end": _iso(next_market["end_date"]),
                }
                if next_market is not None
                else None
            ),
            "chainlink": {
                "source": f"Polymarket RTDS Chainlink {profile.reference_symbol} {window}s TWAP",
                "symbol": profile.reference_symbol,
                "window_seconds": window,
                "current": _value(current.price) if current else None,
                "start": _value(opening.price) if opening else None,
                "last_update": _iso(current.source_time) if current else None,
                "freshness_seconds": (now - current.source_time).total_seconds()
                if current
                else None,
                "opening_source": "first_twap_within_tolerance" if opening else None,
                "opening_timestamp": _iso(opening.source_time) if opening else None,
                "opening_verified": False,
                "opening_tolerance_seconds": self._settings.indicator_opening_tolerance_seconds,
            },
            "quality": {
                "status": "ok" if not issues else "degraded",
                "issues": issues,
                "sample_count": analysis.sample_count,
            },
            "model": (
                {
                    "up_probability": str(estimate.up_probability),
                    "down_probability": str(Decimal("1") - estimate.up_probability),
                    "distance_usd": str(estimate.distance_usd),
                    "momentum_15s": str(estimate.momentum_15s),
                    "volatility_60s": str(estimate.volatility_60s),
                    "expected_remaining_move_usd": str(estimate.expected_remaining_move_usd),
                    "status": "experimental_not_calibrated",
                    "strategy_id": profile.strategy_id,
                }
                if estimate is not None
                else None
            ),
            "outcomes": outcomes,
            "research": research,
        }

    @staticmethod
    def _microstructure(
        connection: Connection, yes_token: str, no_token: str, now: datetime
    ) -> dict[str, dict[str, Any]]:
        """Three batched reads for both outcomes."""
        params = {
            "yes_token": yes_token,
            "no_token": no_token,
            "now": now,
            "quote_cutoff": now - timedelta(seconds=10),
            "trade_cutoff": now - timedelta(seconds=30),
        }
        books = (
            connection.execute(
                text("""
            SELECT DISTINCT ON (token_id) token_id, spread, bid_depth_1pct, ask_depth_1pct,
                   bid_depth_total, ask_depth_total, imbalance, observed_at
            FROM order_book_metrics WHERE token_id IN (:yes_token, :no_token)
              AND observed_at <= :now
            ORDER BY token_id, observed_at DESC, id DESC
        """),
                params,
            )
            .mappings()
            .all()
        )
        velocities = (
            connection.execute(
                text("""
            SELECT instrument, count(*) / 10.0 AS updates_per_second,
                   max(best_ask) - min(best_ask) AS ask_range,
                   max(best_bid) - min(best_bid) AS bid_range
            FROM price_ticks WHERE instrument IN (:yes_token, :no_token)
              AND observed_at >= :quote_cutoff AND observed_at <= :now
            GROUP BY instrument
        """),
                params,
            )
            .mappings()
            .all()
        )
        trades = (
            connection.execute(
                text("""
            SELECT token_id, coalesce(sum(size) FILTER (WHERE side = 'BUY'), 0) AS buy_size,
                   coalesce(sum(size) FILTER (WHERE side = 'SELL'), 0) AS sell_size,
                   count(*) AS trade_count
            FROM market_trade_ticks WHERE token_id IN (:yes_token, :no_token)
              AND observed_at >= :trade_cutoff AND observed_at <= :now
            GROUP BY token_id
        """),
                params,
            )
            .mappings()
            .all()
        )
        result: dict[str, dict[str, Any]] = {
            token: {
                "book": None,
                "book_updated_at": None,
                "book_age_seconds": None,
                "quote_velocity": {
                    "updates_per_second": "0",
                    "ask_range_10s": "0",
                    "bid_range_10s": "0",
                },
                "trade_flow_30s": {"buy_size": "0", "sell_size": "0", "trade_count": 0},
            }
            for token in (yes_token, no_token)
        }
        for book in books:
            result[book["token_id"]]["book"] = {
                key: _value(book[key])
                for key in (
                    "spread",
                    "bid_depth_1pct",
                    "ask_depth_1pct",
                    "bid_depth_total",
                    "ask_depth_total",
                    "imbalance",
                )
            }
            result[book["token_id"]]["book_updated_at"] = _iso(book["observed_at"])
            result[book["token_id"]]["book_age_seconds"] = (
                now - book["observed_at"]
            ).total_seconds()
        for velocity in velocities:
            result[velocity["instrument"]]["quote_velocity"] = {
                "updates_per_second": str(velocity["updates_per_second"]),
                "ask_range_10s": str(velocity["ask_range"] or 0),
                "bid_range_10s": str(velocity["bid_range"] or 0),
            }
        for trade in trades:
            result[trade["token_id"]]["trade_flow_30s"] = {
                "buy_size": str(trade["buy_size"]),
                "sell_size": str(trade["sell_size"]),
                "trade_count": int(trade["trade_count"]),
            }
        return result
