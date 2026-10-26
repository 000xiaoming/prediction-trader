"""Point-in-time SQL, additive migration, and settlement ingestion on disposable PG."""

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from prediction_trader.adapters.polymarket import DiscoveredMarket, GammaClient
from prediction_trader.backtesting.engine import BacktestParameters
from prediction_trader.backtesting.repository import BacktestRepository, profile_backtest_runs
from prediction_trader.persistence import (
    MarketRepository,
    market_metadata_observations,
    market_settlements,
    markets,
    price_ticks,
    reference_prices,
    replay_fill_settlements,
    replay_fills,
)
from prediction_trader.profiles import MarketProfile, get_profile, list_profiles
from prediction_trader.settlement import SettlementSynchronizer

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
SOURCE = "polymarket_rtds_chainlink_twap_60"
BTC = get_profile("polymarket_btc_5m")


@pytest.fixture
def repository() -> Iterator[MarketRepository]:
    database_url = os.environ.get("PREDICTION_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PREDICTION_TEST_DATABASE_URL is not configured")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("Replay integration tests require disposable PostgreSQL")
    schema = f"backtest_test_{uuid4().hex}"
    admin = MarketRepository(database_url)
    with admin.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    scoped = MarketRepository(scoped_url.render_as_string(hide_password=False))
    try:
        scoped.create_schema()
        yield scoped
    finally:
        scoped.close()
        with admin.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.close()


def _params() -> BacktestParameters:
    return BacktestParameters(
        start=START,
        end=START + timedelta(minutes=15),
        as_of=START + timedelta(hours=1),
        slippage_bps=Decimal("0"),
    )


def _row(profile: MarketProfile) -> dict[str, object]:
    return {
        "id": profile.id,
        "event_id": f"event-{profile.id}",
        "slug": profile.slug_at(START),
        "question": profile.label,
        "condition_id": f"condition-{profile.id}",
        "yes_token_id": f"up-{profile.id}",
        "no_token_id": f"down-{profile.id}",
        "end_date": START + timedelta(seconds=profile.duration_seconds),
        "raw": {"feeSchedule": {"rate": "0.07"}},
        "updated_at": START,
    }


def _seed(repository: MarketRepository) -> None:
    with repository.engine.begin() as connection:
        for profile in list_profiles():
            connection.execute(markets.insert().values(**_row(profile)))
            connection.execute(
                price_ticks.insert(),
                [
                    {
                        "venue": "polymarket",
                        "instrument": f"up-{profile.id}",
                        "market_id": profile.id,
                        "outcome": "YES",
                        "best_ask": Decimal("0.4"),
                        "source_timestamp": START + timedelta(seconds=second),
                        "observed_at": START + timedelta(seconds=second),
                    }
                    for second in (59, 61)
                ],
            )
            connection.execute(
                market_settlements.insert().values(
                    market_id=profile.id,
                    winning_outcome="YES",
                    resolved_at=START + timedelta(seconds=profile.duration_seconds + 10),
                    observed_at=START + timedelta(seconds=profile.duration_seconds + 20),
                    source="test",
                    raw={},
                )
            )
        for symbol, base in (("BTC/USD", 100000), ("ETH/USD", 2000)):
            connection.execute(
                reference_prices.insert(),
                [
                    {
                        "source": SOURCE,
                        "symbol": symbol,
                        "price": Decimal(base) + Decimal(second) / 10,
                        "source_timestamp": START + timedelta(seconds=second),
                        "observed_at": START + timedelta(seconds=second),
                    }
                    for second in range(61)
                ],
            )


def test_reports_are_persisted_and_latest_is_isolated_by_profile(
    repository: MarketRepository,
) -> None:
    _seed(repository)
    backtests = BacktestRepository(repository)
    backtests.create_schema()
    assert backtests.latest(BTC.id) is None
    run_ids = []
    for profile in list_profiles():
        report = backtests.run(profile, _params())
        run_ids.append(report["run_id"])
        assert report["metrics"]["market_count"] == 1
        assert report["metrics"]["fill_count"] == 1
        assert report["metrics"]["resolved_prediction_count"] == 1
        assert report["decisions"][0]["market_id"] == profile.id
        assert backtests.latest(profile.id) == report
    assert len(set(run_ids)) == 3
    with repository.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(profile_backtest_runs)) == 3


def test_repository_filters_asset_source_and_delayed_observation(
    repository: MarketRepository,
) -> None:
    _seed(repository)
    with repository.engine.begin() as connection:
        connection.execute(
            reference_prices.update()
            .where(
                reference_prices.c.symbol == "ETH/USD",
                reference_prices.c.source_timestamp <= START + timedelta(seconds=2),
            )
            .values(observed_at=START + timedelta(seconds=61))
        )
    report = BacktestRepository(repository).run(get_profile("polymarket_eth_5m"), _params())
    assert report["metrics"]["prediction_count"] == 0
    assert "opening_reference_missing" in report["metrics"]["issues"]


def test_latest_eligible_metadata_snapshot_wins_over_future_raw(
    repository: MarketRepository,
) -> None:
    _seed(repository)
    with repository.engine.begin() as connection:
        connection.execute(
            markets.update()
            .where(markets.c.id == BTC.id)
            .values(
                raw={"feeSchedule": {"rate": "1"}},
                updated_at=START + timedelta(minutes=4),
            )
        )
        connection.execute(
            market_metadata_observations.insert(),
            [
                {"market_id": BTC.id, "observed_at": START, "raw": {"feesEnabled": False}},
                {
                    "market_id": BTC.id,
                    "observed_at": START + timedelta(seconds=61),
                    "raw": {"feeSchedule": {"rate": "1"}},
                },
            ],
        )
    report = BacktestRepository(repository).run(BTC, _params())
    assert report["metrics"]["fill_count"] == 1
    assert Decimal(report["metrics"]["total_fees_usd"]) == 0


def test_sql_does_not_use_settlement_unknown_at_as_of(repository: MarketRepository) -> None:
    _seed(repository)
    with repository.engine.begin() as connection:
        connection.execute(
            market_settlements.update().values(
                observed_at=START + timedelta(days=1),
            )
        )
    report = BacktestRepository(repository).run(BTC, _params())
    assert report["metrics"]["unresolved_fill_count"] == 1
    assert report["metrics"]["brier_score"] is None


def test_legacy_settlement_migration_assigns_actual_migration_time(
    repository: MarketRepository,
) -> None:
    _seed(repository)
    with repository.engine.begin() as connection:
        connection.execute(text("ALTER TABLE market_settlements DROP COLUMN observed_at"))
    before = datetime.now(UTC)
    repository.create_schema()
    repository.create_schema()
    with repository.engine.connect() as connection:
        row = connection.execute(select(market_settlements)).mappings().first()
    assert row is not None and row["observed_at"] >= before
    assert row["observed_at"] != row["resolved_at"]


def test_discovery_keeps_first_unchanged_metadata_observation(repository: MarketRepository) -> None:
    item = DiscoveredMarket(
        event_id="event",
        event_slug="event-slug",
        event_title="test",
        market_id="market",
        market_slug=BTC.slug_at(START),
        question="test",
        condition_id="condition",
        yes_token_id="up",
        no_token_id="down",
        end_date=START + timedelta(minutes=5),
        raw={"feeSchedule": {"rate": "0.07"}},
    )
    repository.upsert([item])
    repository.upsert([item])
    repository.upsert([replace(item, raw={"feesEnabled": False})])
    with repository.engine.connect() as connection:
        rows = (
            connection.execute(
                select(market_metadata_observations).order_by(
                    market_metadata_observations.c.id,
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    assert rows[0]["raw"] == item.raw
    assert rows[1]["raw"] == {"feesEnabled": False}
    assert rows[0]["observed_at"] <= rows[1]["observed_at"]


def test_settlement_sync_covers_discovered_markets_and_uses_returning_count(
    repository: MarketRepository,
) -> None:
    now = datetime.now(UTC)
    ended_at = now - timedelta(minutes=10)
    with repository.engine.begin() as connection:
        for profile in list_profiles():
            row = _row(profile)
            row["end_date"] = ended_at
            connection.execute(markets.insert().values(**row))
        connection.execute(
            replay_fills.insert().values(
                run_id="legacy-run",
                market_id=BTC.id,
                outcome="YES",
                signal_time=ended_at,
                fill_time=ended_at,
                fill_price=Decimal("0.4"),
                notional_usd=Decimal("10"),
                shares=Decimal("25"),
            )
        )
    client = Mock(spec=GammaClient)
    client.get_market.side_effect = lambda market_id: {
        "id": market_id,
        "closed": True,
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["1", "0"],
        "closedTime": ended_at.isoformat(),
    }
    before = datetime.now(UTC)
    result = SettlementSynchronizer(repository, client).sync()
    assert result.checked_markets == result.resolved_markets == 3
    assert result.newly_settled_fills == 1
    again = SettlementSynchronizer(repository, client).sync()
    assert again.newly_settled_fills == again.checked_markets == 0
    with repository.engine.connect() as connection:
        settlements = connection.execute(select(market_settlements)).mappings().all()
        assert len(settlements) == 3
        assert all(row["observed_at"] >= before for row in settlements)
        assert connection.scalar(select(func.count()).select_from(replay_fill_settlements)) == 1


def test_new_replay_fill_settles_against_previously_observed_outcome(
    repository: MarketRepository,
) -> None:
    _seed(repository)
    with repository.engine.begin() as connection:
        connection.execute(
            replay_fills.insert().values(
                run_id="later-run",
                market_id=BTC.id,
                outcome="NO",
                signal_time=START,
                fill_time=START,
                fill_price=Decimal("0.4"),
                notional_usd=Decimal("10"),
                shares=Decimal("25"),
            )
        )
    client = Mock(spec=GammaClient)
    result = SettlementSynchronizer(repository, client).sync()
    assert result.newly_settled_fills == 1
    assert client.get_market.call_count == 0


def test_settlement_batch_prioritizes_new_market_over_old_unresolved_backlog(
    repository: MarketRepository,
) -> None:
    now = datetime.now(UTC)
    rows = []
    for index in range(205):
        row = _row(BTC)
        row.update(
            id=f"old-{index}",
            yes_token_id=f"up-{index}",
            no_token_id=f"down-{index}",
            end_date=now - timedelta(minutes=1000 + index),
        )
        rows.append(row)
    fresh = _row(BTC)
    fresh["end_date"] = now - timedelta(seconds=5)
    rows.append(fresh)
    with repository.engine.begin() as connection:
        connection.execute(markets.insert(), rows)
    client = Mock(spec=GammaClient)
    client.get_market.side_effect = lambda market_id: {
        "id": market_id,
        "closed": market_id == BTC.id,
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["1", "0"],
        "closedTime": now.isoformat(),
    }
    result = SettlementSynchronizer(repository, client).sync()
    assert result.checked_markets == 200
    assert result.resolved_markets == 1
    assert client.get_market.call_args_list[0].args == (BTC.id,)
    assert result.newly_settled_fills == 0


def test_settlement_response_cannot_resolve_a_different_market(
    repository: MarketRepository,
) -> None:
    row = _row(BTC)
    row["end_date"] = datetime.now(UTC) - timedelta(minutes=1)
    with repository.engine.begin() as connection:
        connection.execute(markets.insert().values(**row))
    client = Mock(spec=GammaClient)
    client.get_market.return_value = {
        "id": "wrong-market",
        "closed": True,
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["1", "0"],
    }
    assert SettlementSynchronizer(repository, client).sync().resolved_markets == 0


def test_oversized_event_history_is_flagged_not_partially_replayed(
    repository: MarketRepository,
) -> None:
    _seed(repository)
    with patch("prediction_trader.backtesting.repository.MAX_EVENTS_PER_MARKET", 2):
        report = BacktestRepository(repository).run(BTC, _params())
    assert report["metrics"]["prediction_count"] == 0
    assert report["metrics"]["issues"] == {"event_limit_exceeded": 1}
