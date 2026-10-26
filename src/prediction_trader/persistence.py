import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Identity,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from prediction_trader.adapters.polymarket import DiscoveredMarket
from prediction_trader.profiles import MarketProfile

metadata = MetaData()

events = Table(
    "events",
    metadata,
    Column("id", String, primary_key=True),
    Column("slug", String, nullable=False),
    Column("title", String, nullable=False),
    Column("source", String, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

markets = Table(
    "markets",
    metadata,
    Column("id", String, primary_key=True),
    Column("event_id", String, nullable=False, index=True),
    Column("slug", String, nullable=False),
    Column("question", String, nullable=False),
    Column("condition_id", String, nullable=False),
    Column("yes_token_id", String, nullable=False, unique=True),
    Column("no_token_id", String, nullable=False, unique=True),
    Column("end_date", DateTime(timezone=True)),
    Column("raw", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

market_metadata_observations = Table(
    "market_metadata_observations",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("market_id", String, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("raw", JSON, nullable=False),
    Index("ix_market_metadata_observations_market_time", "market_id", "observed_at", "id"),
)

market_quotes = Table(
    "market_quotes",
    metadata,
    Column("token_id", String, primary_key=True),
    Column("market_id", String, nullable=False, index=True),
    Column("outcome", String, nullable=False),
    Column("best_bid", Numeric(20, 10)),
    Column("best_ask", Numeric(20, 10)),
    Column("mid_price", Numeric(20, 10)),
    Column("last_trade_price", Numeric(20, 10)),
    Column("source_timestamp", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

price_ticks = Table(
    "price_ticks",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("venue", String, nullable=False, index=True),
    Column("instrument", String, nullable=False, index=True),
    Column("market_id", String),
    Column("outcome", String),
    Column("best_bid", Numeric(24, 10)),
    Column("best_ask", Numeric(24, 10)),
    Column("mid_price", Numeric(24, 10)),
    Column("last_trade_price", Numeric(24, 10)),
    Column("source_timestamp", DateTime(timezone=True)),
    Column("observed_at", DateTime(timezone=True), nullable=False, index=True),
)

price_candles_1m = Table(
    "price_candles_1m",
    metadata,
    Column("venue", String, primary_key=True),
    Column("instrument", String, primary_key=True),
    Column("bucket", DateTime(timezone=True), primary_key=True),
    Column("open", Numeric(24, 10), nullable=False),
    Column("high", Numeric(24, 10), nullable=False),
    Column("low", Numeric(24, 10), nullable=False),
    Column("close", Numeric(24, 10), nullable=False),
    Column("samples", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

replay_runs = Table(
    "replay_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("strategy_name", String, nullable=False, index=True),
    Column("mode", String, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("frames", BigInteger, nullable=False),
    Column("signal_count", BigInteger, nullable=False),
    Column("fill_count", BigInteger, nullable=False),
    Column("unfilled_count", BigInteger, nullable=False),
    Column("marked_pnl_usd", Numeric(24, 10), nullable=False),
    Column("max_drawdown_usd", Numeric(24, 10), nullable=False),
    Column("parameters", JSON, nullable=False),
)

replay_signals = Table(
    "replay_signals",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("strategy_name", String, nullable=False),
    Column("strategy_group", String, nullable=False),
    Column("market_id", String, nullable=False, index=True),
    Column("yes_probability", Numeric(20, 10), nullable=False),
    Column("confidence", Numeric(20, 10), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False, index=True),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("filled", Boolean, nullable=False),
    Column("evidence", JSON, nullable=False),
)

replay_fills = Table(
    "replay_fills",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("market_id", String, nullable=False, index=True),
    Column("outcome", String, nullable=False),
    Column("signal_time", DateTime(timezone=True), nullable=False),
    Column("fill_time", DateTime(timezone=True), nullable=False, index=True),
    Column("fill_price", Numeric(20, 10), nullable=False),
    Column("mark_price", Numeric(20, 10)),
    Column("notional_usd", Numeric(24, 10), nullable=False),
    Column("shares", Numeric(24, 10), nullable=False),
    Column("unrealized_pnl_usd", Numeric(24, 10)),
)

replay_equity_points = Table(
    "replay_equity_points",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("point_time", DateTime(timezone=True), nullable=False, index=True),
    Column("equity_usd", Numeric(24, 10), nullable=False),
    Column("drawdown_usd", Numeric(24, 10), nullable=False),
)

market_settlements = Table(
    "market_settlements",
    metadata,
    Column("market_id", String, primary_key=True),
    Column("winning_outcome", String, nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("source", String, nullable=False),
    Column("raw", JSON, nullable=False),
)

replay_fill_settlements = Table(
    "replay_fill_settlements",
    metadata,
    Column("fill_id", BigInteger, primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("market_id", String, nullable=False, index=True),
    Column("winning_outcome", String, nullable=False),
    Column("won", Boolean, nullable=False),
    Column("payout_usd", Numeric(24, 10), nullable=False),
    Column("realized_pnl_usd", Numeric(24, 10), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=False, index=True),
)

reference_prices = Table(
    "reference_prices",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("source", String, nullable=False, index=True),
    Column("symbol", String, nullable=False, index=True),
    Column("price", Numeric(24, 10), nullable=False),
    Column("source_timestamp", DateTime(timezone=True), nullable=False, index=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
)

order_book_metrics = Table(
    "order_book_metrics",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("token_id", String, nullable=False, index=True),
    Column("market_id", String, nullable=False, index=True),
    Column("outcome", String, nullable=False),
    Column("best_bid", Numeric(20, 10)),
    Column("best_ask", Numeric(20, 10)),
    Column("spread", Numeric(20, 10)),
    Column("bid_depth_1pct", Numeric(24, 10), nullable=False),
    Column("ask_depth_1pct", Numeric(24, 10), nullable=False),
    Column("bid_depth_total", Numeric(24, 10), nullable=False),
    Column("ask_depth_total", Numeric(24, 10), nullable=False),
    Column("imbalance", Numeric(20, 10)),
    Column("source_timestamp", DateTime(timezone=True)),
    Column("observed_at", DateTime(timezone=True), nullable=False, index=True),
)

market_trade_ticks = Table(
    "market_trade_ticks",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("token_id", String, nullable=False, index=True),
    Column("market_id", String, nullable=False, index=True),
    Column("outcome", String, nullable=False),
    Column("side", String, nullable=False),
    Column("price", Numeric(20, 10), nullable=False),
    Column("size", Numeric(24, 10), nullable=False),
    Column("fee_rate_bps", Numeric(20, 10)),
    Column("source_timestamp", DateTime(timezone=True)),
    Column("observed_at", DateTime(timezone=True), nullable=False, index=True),
)


@dataclass(frozen=True, slots=True)
class OutcomeToken:
    token_id: str
    market_id: str
    outcome: str


class MarketRepository:
    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(database_url, pool_pre_ping=True)

    def create_schema(self) -> None:
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                # Multiple profile processes can start together. Serialize additive
                # DDL, including SQLAlchemy's otherwise racy check-before-create.
                connection.execute(text("SELECT pg_advisory_xact_lock(2026091901)"))
            metadata.create_all(connection)
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("""
                    ALTER TABLE market_settlements ADD COLUMN IF NOT EXISTS
                    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                """)
                )

    def upsert(self, discovered: list[DiscoveredMarket]) -> int:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            for item in discovered:
                event_values = {
                    "id": item.event_id,
                    "slug": item.event_slug,
                    "title": item.event_title,
                    "source": "polymarket",
                    "updated_at": now,
                }
                event_stmt = insert(events).values(**event_values)
                connection.execute(
                    event_stmt.on_conflict_do_update(
                        index_elements=[events.c.id], set_=event_values
                    )
                )

                market_values = {
                    "id": item.market_id,
                    "event_id": item.event_id,
                    "slug": item.market_slug,
                    "question": item.question,
                    "condition_id": item.condition_id,
                    "yes_token_id": item.yes_token_id,
                    "no_token_id": item.no_token_id,
                    "end_date": item.end_date,
                    "raw": json.loads(json.dumps(item.raw)),
                    "updated_at": now,
                }
                market_stmt = insert(markets).values(**market_values)
                connection.execute(
                    market_stmt.on_conflict_do_update(
                        index_elements=[markets.c.id], set_=market_values
                    )
                )
                previous_raw = connection.execute(
                    select(market_metadata_observations.c.raw)
                    .where(market_metadata_observations.c.market_id == item.market_id)
                    .order_by(
                        market_metadata_observations.c.observed_at.desc(),
                        market_metadata_observations.c.id.desc(),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if previous_raw != market_values["raw"]:
                    connection.execute(
                        market_metadata_observations.insert().values(
                            market_id=item.market_id,
                            observed_at=now,
                            raw=market_values["raw"],
                        )
                    )
        return len(discovered)

    def list_outcome_tokens(
        self, *, profile: MarketProfile | None = None, now: datetime | None = None
    ) -> list[OutcomeToken]:
        if profile is not None:
            checked_at = now or datetime.now(UTC)
            start = profile.window_start(checked_at)
            duration = timedelta(seconds=profile.duration_seconds)
            # Only the active and next window: old profiles/expired markets stay in
            # storage, but never add sockets/subscriptions to the active runtime.
            statement = select(markets.c.id, markets.c.yes_token_id, markets.c.no_token_id).where(
                markets.c.end_date > checked_at,
                or_(
                    (markets.c.slug == profile.slug_at(start))
                    & (markets.c.end_date == start + duration),
                    (markets.c.slug == profile.slug_at(start + duration))
                    & (markets.c.end_date == start + duration * 2),
                ),
            )
            with self.engine.connect() as connection:
                rows = connection.execute(statement).all()
            return [
                token
                for market_id, yes_id, no_id in rows
                for token in (
                    OutcomeToken(str(yes_id), str(market_id), "YES"),
                    OutcomeToken(str(no_id), str(market_id), "NO"),
                )
            ]
        statement = select(markets.c.id, markets.c.yes_token_id, markets.c.no_token_id).where(
            or_(markets.c.end_date.is_(None), markets.c.end_date > func.now())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        result: list[OutcomeToken] = []
        for market_id, yes_token_id, no_token_id in rows:
            result.append(OutcomeToken(str(yes_token_id), str(market_id), "YES"))
            result.append(OutcomeToken(str(no_token_id), str(market_id), "NO"))
        return result

    def upsert_quote(
        self,
        token: OutcomeToken,
        *,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
        last_trade_price: Decimal | None = None,
        source_timestamp: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC)
        mid_price = None
        if best_bid is not None and best_ask is not None:
            mid_price = (best_bid + best_ask) / 2
        insert_values = {
            "token_id": token.token_id,
            "market_id": token.market_id,
            "outcome": token.outcome,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "last_trade_price": last_trade_price,
            "source_timestamp": source_timestamp,
            "updated_at": now,
        }
        update_values = {key: value for key, value in insert_values.items() if value is not None}
        update_values["updated_at"] = now
        statement = insert(market_quotes).values(**insert_values)
        with self.engine.begin() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[market_quotes.c.token_id], set_=update_values
                )
            )

    def append_tick(
        self,
        *,
        venue: str,
        instrument: str,
        best_bid: Decimal | None,
        best_ask: Decimal | None,
        last_trade_price: Decimal | None = None,
        market_id: str | None = None,
        outcome: str | None = None,
        source_timestamp: datetime | None = None,
    ) -> None:
        mid_price = None
        if best_bid is not None and best_ask is not None:
            mid_price = (best_bid + best_ask) / 2
        with self.engine.begin() as connection:
            connection.execute(
                price_ticks.insert().values(
                    venue=venue,
                    instrument=instrument,
                    market_id=market_id,
                    outcome=outcome,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    mid_price=mid_price,
                    last_trade_price=last_trade_price,
                    source_timestamp=source_timestamp,
                    observed_at=datetime.now(UTC),
                )
            )

    def append_reference_price(
        self, *, source: str, symbol: str, price: Decimal, source_timestamp: datetime
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                reference_prices.insert().values(
                    source=source,
                    symbol=symbol,
                    price=price,
                    source_timestamp=source_timestamp,
                    observed_at=datetime.now(UTC),
                )
            )

    def append_book_metrics(
        self,
        *,
        token: OutcomeToken,
        best_bid: Decimal | None,
        best_ask: Decimal | None,
        bid_depth_1pct: Decimal,
        ask_depth_1pct: Decimal,
        bid_depth_total: Decimal,
        ask_depth_total: Decimal,
        source_timestamp: datetime | None,
    ) -> None:
        spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
        total_near = bid_depth_1pct + ask_depth_1pct
        imbalance = None if total_near == 0 else (bid_depth_1pct - ask_depth_1pct) / total_near
        with self.engine.begin() as connection:
            connection.execute(
                order_book_metrics.insert().values(
                    token_id=token.token_id,
                    market_id=token.market_id,
                    outcome=token.outcome,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    spread=spread,
                    bid_depth_1pct=bid_depth_1pct,
                    ask_depth_1pct=ask_depth_1pct,
                    bid_depth_total=bid_depth_total,
                    ask_depth_total=ask_depth_total,
                    imbalance=imbalance,
                    source_timestamp=source_timestamp,
                    observed_at=datetime.now(UTC),
                )
            )

    def append_market_trade(
        self,
        *,
        token: OutcomeToken,
        side: str,
        price: Decimal,
        size: Decimal,
        fee_rate_bps: Decimal | None,
        source_timestamp: datetime | None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                market_trade_ticks.insert().values(
                    token_id=token.token_id,
                    market_id=token.market_id,
                    outcome=token.outcome,
                    side=side,
                    price=price,
                    size=size,
                    fee_rate_bps=fee_rate_bps,
                    source_timestamp=source_timestamp,
                    observed_at=datetime.now(UTC),
                )
            )

    def refresh_one_minute_candles(self) -> int:
        statement = text(
            """
            INSERT INTO price_candles_1m
                (venue, instrument, bucket, open, high, low, close, samples, updated_at)
            SELECT venue, instrument, bucket,
                   (array_agg(price ORDER BY observed_at, id))[1] AS open,
                   max(price) AS high,
                   min(price) AS low,
                   (array_agg(price ORDER BY observed_at DESC, id DESC))[1] AS close,
                   count(*) AS samples,
                   now() AS updated_at
            FROM (
                SELECT id, venue, instrument, observed_at,
                       date_trunc('minute', observed_at) AS bucket,
                       coalesce(mid_price, last_trade_price, best_bid, best_ask) AS price
                FROM price_ticks
                WHERE observed_at >= now() - interval '2 hours'
            ) ticks
            WHERE price IS NOT NULL
            GROUP BY venue, instrument, bucket
            ON CONFLICT (venue, instrument, bucket) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                samples = EXCLUDED.samples,
                updated_at = EXCLUDED.updated_at
            """
        )
        with self.engine.begin() as connection:
            result = connection.execute(statement)
        return result.rowcount

    def close(self) -> None:
        self.engine.dispose()
