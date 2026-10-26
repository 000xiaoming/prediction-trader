"""Bounded historical reads and independent storage for per-profile reports."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from prediction_trader.backtesting.engine import (
    BacktestParameters,
    HistoricalMarket,
    HistoricalQuote,
    HistoricalReference,
    HistoricalSettlement,
    replay_profile,
)
from prediction_trader.persistence import (
    MarketRepository,
    market_metadata_observations,
    market_settlements,
    markets,
    price_ticks,
    reference_prices,
)
from prediction_trader.profiles import MarketProfile, get_profile

metadata = MetaData()
profile_backtest_runs = Table(
    "profile_backtest_runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("profile_id", String, nullable=False, index=True),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("parameters", JSON, nullable=False),
    Column("report", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
)
settlement_observations = Table(
    "profile_backtest_settlement_observations",
    metadata,
    Column("market_id", String, primary_key=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
)

MAX_EVENTS_PER_MARKET = 20000


class BacktestRepository:
    def __init__(self, repository: MarketRepository) -> None:
        self._repository = repository

    def create_schema(self) -> None:
        with self._repository.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(2026091901)"))
            metadata.create_all(connection, checkfirst=True)

    def latest(self, profile_id: str) -> dict[str, Any] | None:
        get_profile(profile_id)
        with self._repository.engine.connect() as connection:
            report = connection.execute(
                select(profile_backtest_runs.c.report)
                .where(profile_backtest_runs.c.profile_id == profile_id)
                .order_by(profile_backtest_runs.c.created_at.desc(), profile_backtest_runs.c.run_id)
                .limit(1)
            ).scalar_one_or_none()
        return dict(report) if isinstance(report, dict) else None

    def run(self, profile: MarketProfile, parameters: BacktestParameters) -> dict[str, Any]:
        parameters.validate_profile(profile)
        self.create_schema()
        self._capture_settlement_observations(profile, parameters)
        report = replay_profile(profile, parameters, self._history(profile, parameters))
        now = datetime.now(UTC)
        report["run_id"] = uuid4().hex
        report["created_at"] = now.isoformat()
        with self._repository.engine.begin() as connection:
            connection.execute(
                profile_backtest_runs.insert().values(
                    run_id=report["run_id"],
                    profile_id=profile.id,
                    as_of=parameters.as_of,
                    parameters=parameters.as_dict(),
                    report=report,
                    created_at=now,
                )
            )
        return report

    def _capture_settlement_observations(
        self,
        profile: MarketProfile,
        parameters: BacktestParameters,
    ) -> None:
        # Legacy settlement rows do not record local availability. The first replay
        # that sees one can only attest availability now, never at resolved_at.
        duration = timedelta(seconds=profile.duration_seconds)
        statement = (
            insert(settlement_observations)
            .from_select(
                ["market_id", "observed_at"],
                select(market_settlements.c.market_id, text("CURRENT_TIMESTAMP"))
                .join(markets, markets.c.id == market_settlements.c.market_id)
                .where(
                    markets.c.slug.startswith(f"{profile.slug_prefix}-", autoescape=True),
                    markets.c.end_date >= parameters.start + duration,
                    markets.c.end_date < parameters.end + duration,
                ),
            )
            .on_conflict_do_nothing(index_elements=[settlement_observations.c.market_id])
        )
        with self._repository.engine.begin() as connection:
            connection.execute(statement)

    def _history(
        self,
        profile: MarketProfile,
        parameters: BacktestParameters,
    ) -> Iterator[HistoricalMarket]:
        duration = timedelta(seconds=profile.duration_seconds)
        with self._repository.engine.connect().execution_options(
            isolation_level="REPEATABLE READ",
        ) as connection:
            market_rows = connection.execute(
                select(markets)
                .where(
                    markets.c.slug.startswith(f"{profile.slug_prefix}-", autoescape=True),
                    markets.c.end_date >= parameters.start + duration,
                    markets.c.end_date < parameters.end + duration,
                )
                .order_by(markets.c.end_date, markets.c.id)
                .execution_options(stream_results=True, yield_per=100)
            ).mappings()
            loaded = 0
            for row in market_rows:
                end: datetime = row["end_date"]
                if not profile.matches_market(str(row["slug"]), end):
                    continue
                loaded += 1
                if loaded > parameters.max_markets:
                    # One sentinel market lets the engine mark truncation without
                    # reading its event history or returning a partial market.
                    yield HistoricalMarket(
                        market_id=str(row["id"]),
                        slug=str(row["slug"]),
                        end=end,
                        yes_token_id=str(row["yes_token_id"]),
                        no_token_id=str(row["no_token_id"]),
                        raw={},
                        metadata_observed_at=row["updated_at"],
                    )
                    break
                start = end - duration
                decision_at = start + timedelta(seconds=parameters.decision_delay_seconds)
                fee_snapshot = (
                    connection.execute(
                        select(market_metadata_observations)
                        .where(
                            market_metadata_observations.c.market_id == row["id"],
                            market_metadata_observations.c.observed_at <= decision_at,
                        )
                        .order_by(
                            market_metadata_observations.c.observed_at.desc(),
                            market_metadata_observations.c.id.desc(),
                        )
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                refs = (
                    connection.execute(
                        select(reference_prices)
                        .where(
                            reference_prices.c.source
                            == (f"polymarket_rtds_chainlink_twap_{parameters.twap_window_seconds}"),
                            reference_prices.c.symbol == profile.reference_symbol,
                            reference_prices.c.source_timestamp
                            >= min(
                                start,
                                decision_at - timedelta(seconds=60),
                            ),
                            reference_prices.c.source_timestamp
                            <= min(decision_at, parameters.as_of),
                            reference_prices.c.observed_at <= min(decision_at, parameters.as_of),
                        )
                        .order_by(
                            reference_prices.c.source_timestamp,
                            reference_prices.c.observed_at,
                            reference_prices.c.id,
                        )
                        .limit(MAX_EVENTS_PER_MARKET + 1)
                    )
                    .mappings()
                    .all()
                )
                deadline = min(
                    decision_at
                    + timedelta(
                        milliseconds=parameters.latency_ms,
                        seconds=parameters.timeout_seconds,
                    ),
                    end,
                    parameters.as_of,
                )
                quote_rows = (
                    connection.execute(
                        select(price_ticks)
                        .where(
                            price_ticks.c.venue == "polymarket",
                            price_ticks.c.market_id == str(row["id"]),
                            price_ticks.c.instrument.in_([row["yes_token_id"], row["no_token_id"]]),
                            price_ticks.c.observed_at
                            >= max(
                                start,
                                decision_at - timedelta(seconds=parameters.quote_max_age_seconds),
                            ),
                            price_ticks.c.observed_at <= deadline,
                        )
                        .order_by(price_ticks.c.observed_at, price_ticks.c.id)
                        .limit(MAX_EVENTS_PER_MARKET + 1)
                    )
                    .mappings()
                    .all()
                )
                issues: tuple[str, ...] = ()
                if len(refs) > MAX_EVENTS_PER_MARKET or len(quote_rows) > MAX_EVENTS_PER_MARKET:
                    issues = ("event_limit_exceeded",)
                yield HistoricalMarket(
                    market_id=str(row["id"]),
                    slug=str(row["slug"]),
                    end=end,
                    yes_token_id=str(row["yes_token_id"]),
                    no_token_id=str(row["no_token_id"]),
                    raw=dict(row["raw"]) if isinstance(row["raw"], dict) else {},
                    metadata_observed_at=row["updated_at"],
                    fee_raw=(dict(fee_snapshot["raw"]) if fee_snapshot is not None else None),
                    fee_observed_at=(
                        fee_snapshot["observed_at"] if fee_snapshot is not None else None
                    ),
                    references=tuple(
                        HistoricalReference(
                            source=str(ref["source"]),
                            symbol=str(ref["symbol"]),
                            source_time=ref["source_timestamp"],
                            observed_at=ref["observed_at"],
                            price=Decimal(ref["price"]),
                        )
                        for ref in refs
                    )
                    if not issues
                    else (),
                    quotes=tuple(
                        HistoricalQuote(
                            token_id=str(quote["instrument"]),
                            outcome=str(quote["outcome"]),
                            observed_at=quote["observed_at"],
                            best_ask=Decimal(quote["best_ask"])
                            if quote["best_ask"] is not None
                            else None,
                            source_time=quote["source_timestamp"],
                            sequence=int(quote["id"]),
                        )
                        for quote in quote_rows
                    )
                    if not issues
                    else (),
                    settlement=self._settlement(connection, str(row["id"])),
                    data_issues=issues,
                )

    @staticmethod
    def _settlement(connection: Connection, market_id: str) -> HistoricalSettlement | None:
        # SELECT * also supports installations that have added observed_at to the
        # original settlement schema; old installations use our observation ledger.
        row = (
            connection.execute(
                text("SELECT * FROM market_settlements WHERE market_id = :market_id"),
                {"market_id": market_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        observed_at = row.get("observed_at")
        if not isinstance(observed_at, datetime):
            observed_at = connection.execute(
                select(settlement_observations.c.observed_at).where(
                    settlement_observations.c.market_id == market_id
                )
            ).scalar_one_or_none()
        if not isinstance(observed_at, datetime):
            return None
        return HistoricalSettlement(str(row["winning_outcome"]), row["resolved_at"], observed_at)
