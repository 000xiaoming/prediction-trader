"""Real PostgreSQL coverage; opt in with PREDICTION_TEST_DATABASE_URL.

Every test creates and drops only its own UUID-named schema. Supply a disposable
test database, never the production connection string.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from prediction_trader.config import Settings
from prediction_trader.indicator_state import IndicatorStateReader
from prediction_trader.persistence import (
    MarketRepository,
    market_quotes,
    market_trade_ticks,
    markets,
    order_book_metrics,
    price_ticks,
    reference_prices,
)
from prediction_trader.profiles import get_profile

NOW = datetime(2026, 9, 19, 12, 2, tzinfo=UTC)
START = NOW.replace(minute=0)
END = START + timedelta(minutes=5)
SOURCE = "polymarket_rtds_chainlink_twap_60"


@pytest.fixture
def repository() -> Iterator[MarketRepository]:
    database_url = os.environ.get("PREDICTION_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PREDICTION_TEST_DATABASE_URL is not configured")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("Indicator integration tests require PostgreSQL")
    schema = f"indicator_test_{uuid4().hex}"
    admin = MarketRepository(database_url)
    with admin.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    scoped = MarketRepository(scoped_url.render_as_string(hide_password=False))
    try:
        scoped.create_schema()
        _seed(scoped)
        yield scoped
    finally:
        scoped.close()
        with admin.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.close()


def _seed(repository: MarketRepository) -> None:
    bucket = int(START.timestamp())
    with repository.engine.begin() as connection:
        for name, slug, end_time in (
            ("current", f"btc-updown-5m-{bucket}", END),
            ("next", f"btc-updown-5m-{bucket + 300}", END + timedelta(minutes=5)),
            ("unrelated", f"btc-updown-15m-{bucket}", NOW + timedelta(minutes=1)),
        ):
            connection.execute(
                markets.insert().values(
                    id=name,
                    event_id=f"event-{name}",
                    slug=slug,
                    question=f"Bitcoin Up or Down - {name}",
                    condition_id=f"condition-{name}",
                    yes_token_id=f"{name}-up",
                    no_token_id=f"{name}-down",
                    end_date=end_time,
                    raw={
                        "cryptoMarketConfig": {"twapLookbackSeconds": 60},
                        "feeSchedule": {"rate": 0.06},
                    },
                    updated_at=START,
                )
            )
        reference_rows = [
            {
                "source": SOURCE,
                "symbol": "BTC/USD",
                "price": Decimal("100000") + Decimal(second) / 10,
                "source_timestamp": START + timedelta(seconds=second),
                "observed_at": START + timedelta(seconds=second),
            }
            for second in range(121)
        ]
        for source, symbol, source_time, observed in (
            (SOURCE, "BTC/USD", NOW + timedelta(seconds=1), NOW),
            (SOURCE, "BTC/USD", NOW, NOW + timedelta(seconds=1)),
            ("polymarket_rtds_chainlink", "BTC/USD", NOW, NOW),
            ("polymarket_rtds_chainlink_twap_30", "BTC/USD", NOW, NOW),
            (SOURCE, "ETH/USD", NOW, NOW),
            (SOURCE, "BTC/USD", START - timedelta(seconds=1), START),
        ):
            reference_rows.append(
                {
                    "source": source,
                    "symbol": symbol,
                    "price": Decimal("900000"),
                    "source_timestamp": source_time,
                    "observed_at": observed,
                }
            )
        connection.execute(reference_prices.insert(), reference_rows)
        for factor, label, outcome in ((1, "up", "YES"), (2, "down", "NO")):
            token = f"current-{label}"
            connection.execute(
                market_quotes.insert().values(
                    token_id=token,
                    market_id="current",
                    outcome=outcome,
                    best_bid=Decimal("0.45"),
                    best_ask=Decimal("0.47"),
                    last_trade_price=Decimal("0.46"),
                    updated_at=NOW,
                )
            )
            for offset, depth in ((-2, 1), (-1, 100 * factor), (1, 9999)):
                connection.execute(
                    order_book_metrics.insert().values(
                        token_id=token,
                        market_id="current",
                        outcome=outcome,
                        spread=Decimal("0.02"),
                        bid_depth_1pct=depth,
                        ask_depth_1pct=depth * 2,
                        bid_depth_total=depth * 4,
                        ask_depth_total=depth * 5,
                        imbalance=Decimal("-0.2"),
                        observed_at=NOW + timedelta(seconds=offset),
                    )
                )
            for offset, bid in (
                (-11, Decimal("0.99")),
                (-9, Decimal("0.40")),
                (-5, Decimal("0.41")),
                (-1, Decimal("0.42")),
                (1, Decimal("0.99")),
            ):
                connection.execute(
                    price_ticks.insert().values(
                        venue="polymarket",
                        instrument=token,
                        market_id="current",
                        outcome=outcome,
                        best_bid=bid,
                        best_ask=bid + Decimal("0.02"),
                        observed_at=NOW + timedelta(seconds=offset),
                    )
                )
            for offset, side, size in (
                (-31, "BUY", 9999),
                (-20, "BUY", 2 * factor),
                (-10, "SELL", 3 * factor),
                (1, "SELL", 9999),
            ):
                connection.execute(
                    market_trade_ticks.insert().values(
                        token_id=token,
                        market_id="current",
                        outcome=outcome,
                        side=side,
                        price=Decimal("0.46"),
                        size=size,
                        observed_at=NOW + timedelta(seconds=offset),
                    )
                )
        # A row with the current market ID but a different contract's token must not leak in.
        connection.execute(
            market_quotes.insert().values(
                token_id="unrelated-up",
                market_id="current",
                outcome="YES",
                best_bid=Decimal("0.99"),
                best_ask=Decimal("1"),
                updated_at=NOW,
            )
        )


def _reader(repository: MarketRepository) -> IndicatorStateReader:
    return IndicatorStateReader(
        repository,
        settings=Settings(
            _env_file=None,
            polymarket_series_slug_prefix="btc-updown-5m",
            polymarket_chainlink_twap_window_seconds=60,
            indicator_max_reference_age_seconds=5,
            indicator_max_quote_age_seconds=5,
            indicator_opening_tolerance_seconds=2,
            research_context_path="",
        ),
    )


def test_valid_snapshot_uses_seven_queries_and_one_repeatable_read_transaction(
    repository: MarketRepository,
) -> None:
    calls: list[tuple[str, int, str | None]] = []

    def capture(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        calls.append(
            (
                statement,
                id(connection.get_transaction()),
                connection.get_execution_options().get("isolation_level"),
            )
        )

    event.listen(repository.engine, "before_cursor_execute", capture)
    try:
        snapshot = _reader(repository).read(NOW)
    finally:
        event.remove(repository.engine, "before_cursor_execute", capture)

    assert len(calls) == 7
    assert all(statement.lstrip().upper().startswith("SELECT") for statement, _, _ in calls)
    assert len({transaction for _, transaction, _ in calls}) == 1
    assert {isolation for _, _, isolation in calls} == {"REPEATABLE READ"}
    assert set(snapshot) == {
        "now",
        "status",
        "profile",
        "market",
        "next_market",
        "chainlink",
        "quality",
        "model",
        "outcomes",
        "research",
    }
    assert snapshot["status"] == "ok"
    assert snapshot["profile"] == get_profile("polymarket_btc_5m").as_dict()
    assert snapshot["market"]["id"] == "current"
    assert snapshot["market"]["start"] == START.isoformat()
    assert snapshot["market"]["end"] == END.isoformat()
    assert snapshot["market"]["remaining_seconds"] == 180
    assert snapshot["next_market"]["id"] == "next"
    assert snapshot["next_market"]["start"] == END.isoformat()
    assert snapshot["market"]["fee_rate"] == "0.06"
    assert snapshot["market"]["fee_rate_source"] == "market_metadata"
    assert snapshot["research"] == {"status": "disabled", "context": None}
    assert snapshot["quality"] == {"status": "ok", "issues": [], "sample_count": 61}
    assert snapshot["model"] is not None
    assert Decimal(snapshot["chainlink"]["current"]) == Decimal("100012")
    assert Decimal(snapshot["chainlink"]["start"]) == Decimal("100000")
    assert snapshot["chainlink"]["freshness_seconds"] == 0
    assert snapshot["chainlink"]["opening_verified"] is False
    assert snapshot["chainlink"]["window_seconds"] == 60
    assert snapshot["chainlink"]["last_update"] == NOW.isoformat()

    outcomes = {outcome["label"]: outcome for outcome in snapshot["outcomes"]}
    assert set(outcomes) == {"UP", "DOWN"}
    for factor, label in ((1, "UP"), (2, "DOWN")):
        outcome = outcomes[label]
        assert outcome["token_id"] == f"current-{label.lower()}"
        assert outcome["quote_age_seconds"] == 0
        assert outcome["book_age_seconds"] == 1
        assert outcome["quote_status"] == "fresh"
        assert outcome["max_age_seconds"] == 5
        assert Decimal(outcome["book"]["spread"]) == Decimal("0.02")
        assert Decimal(outcome["book"]["bid_depth_1pct"]) == 100 * factor
        assert Decimal(outcome["book"]["ask_depth_total"]) == 500 * factor
        assert outcome["book_updated_at"] == (NOW - timedelta(seconds=1)).isoformat()
        assert Decimal(outcome["quote_velocity"]["updates_per_second"]) == Decimal("0.3")
        assert Decimal(outcome["quote_velocity"]["ask_range_10s"]) == Decimal("0.02")
        assert Decimal(outcome["quote_velocity"]["bid_range_10s"]) == Decimal("0.02")
        assert Decimal(outcome["trade_flow_30s"]["buy_size"]) == 2 * factor
        assert Decimal(outcome["trade_flow_30s"]["sell_size"]) == 3 * factor
        assert outcome["trade_flow_30s"]["trade_count"] == 2


@pytest.mark.parametrize("current_problem", ["absent", "wrong_end"])
def test_next_or_fifteen_minute_contract_never_substitutes_for_current(
    repository: MarketRepository, current_problem: str
) -> None:
    with repository.engine.begin() as connection:
        if current_problem == "absent":
            connection.execute(markets.delete().where(markets.c.id == "current"))
        else:
            connection.execute(
                markets.update()
                .where(markets.c.id == "current")
                .values(end_date=END + timedelta(seconds=1))
            )
    assert _reader(repository).read(NOW) == {
        "now": NOW.isoformat(),
        "status": "waiting_for_market",
        "profile": get_profile("polymarket_btc_5m").as_dict(),
        "research": {"status": "disabled", "context": None},
    }


def test_missing_opening_reference_suppresses_model(repository: MarketRepository) -> None:
    with repository.engine.begin() as connection:
        connection.execute(
            reference_prices.delete().where(
                reference_prices.c.source_timestamp <= START + timedelta(seconds=2)
            )
        )
    snapshot = _reader(repository).read(NOW)
    assert snapshot["model"] is None
    assert snapshot["chainlink"]["start"] is None
    assert snapshot["quality"]["status"] == "degraded"
    assert "opening_reference_missing" in snapshot["quality"]["issues"]


def test_stale_reference_suppresses_model(repository: MarketRepository) -> None:
    with repository.engine.begin() as connection:
        connection.execute(
            reference_prices.delete().where(
                reference_prices.c.source == SOURCE,
                reference_prices.c.source_timestamp > NOW - timedelta(seconds=6),
            )
        )
    snapshot = _reader(repository).read(NOW)
    assert snapshot["model"] is None
    assert snapshot["chainlink"]["freshness_seconds"] == 6
    assert "reference_stale_or_missing" in snapshot["quality"]["issues"]


def test_contract_twap_mismatch_suppresses_model(repository: MarketRepository) -> None:
    with repository.engine.begin() as connection:
        connection.execute(
            markets.update()
            .where(markets.c.id == "current")
            .values(raw={"cryptoMarketConfig": {"twapLookbackSeconds": 30}})
        )
    snapshot = _reader(repository).read(NOW)
    assert snapshot["model"] is None
    assert snapshot["market"]["twap_lookback_seconds"] == 30
    assert snapshot["chainlink"]["window_seconds"] == 60
    assert "contract_twap_window_mismatch" in snapshot["quality"]["issues"]


def test_future_quote_is_not_displayed(repository: MarketRepository) -> None:
    with repository.engine.begin() as connection:
        connection.execute(
            market_quotes.update()
            .where(market_quotes.c.token_id == "current-up")
            .values(updated_at=NOW + timedelta(seconds=1))
        )
    snapshot = _reader(repository).read(NOW)
    assert [outcome["label"] for outcome in snapshot["outcomes"]] == ["DOWN"]
    assert snapshot["quality"]["status"] == "degraded"
    assert "quotes_stale_or_missing" in snapshot["quality"]["issues"]
    assert snapshot["model"] is not None


def test_missing_microstructure_retains_outcome_with_empty_defaults(
    repository: MarketRepository,
) -> None:
    with repository.engine.begin() as connection:
        for table in (order_book_metrics, market_trade_ticks):
            connection.execute(table.delete().where(table.c.token_id == "current-up"))
        connection.execute(price_ticks.delete().where(price_ticks.c.instrument == "current-up"))
    snapshot = _reader(repository).read(NOW)
    outcome = next(outcome for outcome in snapshot["outcomes"] if outcome["label"] == "UP")
    assert outcome["book"] is None
    assert outcome["book_updated_at"] is None
    assert outcome["book_age_seconds"] is None
    assert outcome["quote_status"] == "stale_or_unverified"
    assert snapshot["quality"]["status"] == "degraded"
    assert "quotes_stale_or_missing" in snapshot["quality"]["issues"]
    assert snapshot["model"] is not None
    assert outcome["quote_velocity"] == {
        "updates_per_second": "0",
        "ask_range_10s": "0",
        "bid_range_10s": "0",
    }
    assert outcome["trade_flow_30s"] == {"buy_size": "0", "sell_size": "0", "trade_count": 0}


@pytest.mark.parametrize(
    ("age_seconds", "status"), [(5.0, "fresh"), (5.001, "stale_or_unverified")]
)
def test_quote_freshness_boundary_preserves_reference_model(
    repository: MarketRepository, age_seconds: float, status: str
) -> None:
    reader = _reader(repository)
    baseline = reader.read(NOW)
    with repository.engine.begin() as connection:
        connection.execute(
            market_quotes.update()
            .where(market_quotes.c.token_id == "current-up")
            .values(updated_at=NOW - timedelta(seconds=age_seconds))
        )
    snapshot = reader.read(NOW)
    outcome = next(outcome for outcome in snapshot["outcomes"] if outcome["label"] == "UP")
    assert outcome["quote_age_seconds"] == age_seconds
    assert outcome["book_age_seconds"] == 1
    assert outcome["quote_status"] == status
    assert snapshot["model"] == baseline["model"]
    assert snapshot["model"] is not None
    assert snapshot["quality"]["status"] == ("ok" if status == "fresh" else "degraded")
    assert ("quotes_stale_or_missing" in snapshot["quality"]["issues"]) == (status != "fresh")


def test_recent_trade_does_not_refresh_old_book_or_change_reference_model(
    repository: MarketRepository,
) -> None:
    reader = _reader(repository)
    baseline = reader.read(NOW)
    with repository.engine.begin() as connection:
        connection.execute(
            order_book_metrics.update()
            .where(
                order_book_metrics.c.token_id == "current-up",
                order_book_metrics.c.observed_at <= NOW,
            )
            .values(observed_at=NOW - timedelta(seconds=6))
        )
        connection.execute(
            market_trade_ticks.insert().values(
                token_id="current-up",
                market_id="current",
                outcome="YES",
                side="BUY",
                price=Decimal("0.46"),
                size=7,
                observed_at=NOW,
            )
        )
    snapshot = reader.read(NOW)
    outcome = next(outcome for outcome in snapshot["outcomes"] if outcome["label"] == "UP")
    assert outcome["quote_age_seconds"] == 0
    assert outcome["book_age_seconds"] == 6
    assert outcome["quote_status"] == "stale_or_unverified"
    assert outcome["trade_flow_30s"]["trade_count"] == 3
    assert Decimal(outcome["trade_flow_30s"]["buy_size"]) == 9
    assert snapshot["quality"]["status"] == "degraded"
    assert "quotes_stale_or_missing" in snapshot["quality"]["issues"]
    assert snapshot["model"] == baseline["model"]
    assert snapshot["model"] is not None


def test_missing_quote_degrades_quality_but_preserves_reference_model(
    repository: MarketRepository,
) -> None:
    reader = _reader(repository)
    baseline = reader.read(NOW)
    with repository.engine.begin() as connection:
        connection.execute(market_quotes.delete().where(market_quotes.c.token_id == "current-up"))
    snapshot = reader.read(NOW)
    assert [outcome["label"] for outcome in snapshot["outcomes"]] == ["DOWN"]
    assert snapshot["quality"]["status"] == "degraded"
    assert "quotes_stale_or_missing" in snapshot["quality"]["issues"]
    assert snapshot["model"] == baseline["model"]
    assert snapshot["model"] is not None


def test_online_indicator_index_migration_is_valid_and_idempotent(
    repository: MarketRepository,
) -> None:
    migration = (
        Path(__file__).resolve().parents[1] / "deploy" / "postgres" / "indicator_indexes.sql"
    ).read_text()
    sql = "\n".join(line for line in migration.splitlines() if not line.lstrip().startswith("--"))
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    expected = {
        "ix_indicator_market_window",
        "ix_indicator_reference_window",
        "ix_indicator_book_latest",
        "ix_indicator_tick_window",
        "ix_indicator_trade_window",
    }
    assert len(statements) == len(expected)
    with repository.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        # create_schema may already include these indexes; exercise actual online creation.
        for index_name in expected:
            connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{index_name}"')
        for _ in range(2):
            for statement in statements:
                connection.exec_driver_sql(statement)
        indexes = connection.execute(
            text("""
            SELECT index_class.relname, index_state.indisvalid, index_state.indisready
            FROM pg_index AS index_state
            JOIN pg_class AS index_class ON index_class.oid = index_state.indexrelid
            JOIN pg_namespace AS namespace ON namespace.oid = index_class.relnamespace
            WHERE namespace.nspname = current_schema()
              AND index_class.relname LIKE 'ix_indicator_%'
        """)
        ).all()
    assert {name for name, _, _ in indexes} == expected
    assert all(valid and ready for _, valid, ready in indexes)
