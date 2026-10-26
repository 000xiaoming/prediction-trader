"""Mixed-profile integration coverage against a disposable PostgreSQL database."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from prediction_trader.config import Settings
from prediction_trader.execution_replay_db import load_quote_events
from prediction_trader.indicator_state import IndicatorStateReader
from prediction_trader.persistence import (
    MarketRepository,
    markets,
    price_candles_1m,
    price_ticks,
    reference_prices,
)
from prediction_trader.profiles import MarketProfile, get_profile, list_profiles
from prediction_trader.replay_db import load_signal_replay_frames

NOW = datetime(2026, 9, 19, 12, 7, tzinfo=UTC)
REFERENCE_START = NOW.replace(minute=0)
SOURCE = "polymarket_rtds_chainlink_twap_60"
PROFILE_IDS = [profile.id for profile in list_profiles()]


@pytest.fixture
def repository() -> Iterator[MarketRepository]:
    database_url = os.environ.get("PREDICTION_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PREDICTION_TEST_DATABASE_URL is not configured")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("Profile integration tests require PostgreSQL")
    schema = f"profile_test_{uuid4().hex}"
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


def _market_row(profile: MarketProfile, name: str, offset: int) -> dict[str, object]:
    duration = timedelta(seconds=profile.duration_seconds)
    start = profile.window_start(NOW) + duration * offset
    market_id = f"{profile.id}-{name}"
    return {
        "id": market_id,
        "event_id": f"event-{market_id}",
        "slug": profile.slug_at(start),
        "question": f"{profile.question_prefix} - {name}",
        "condition_id": f"condition-{market_id}",
        "yes_token_id": f"{market_id}-up",
        "no_token_id": f"{market_id}-down",
        "end_date": start + duration,
        "raw": {"cryptoMarketConfig": {"twapLookbackSeconds": 60}},
        "updated_at": REFERENCE_START,
    }


def _seed(repository: MarketRepository) -> None:
    with repository.engine.begin() as connection:
        rows = []
        for profile in list_profiles():
            rows.extend(
                _market_row(profile, name, offset)
                for name, offset in (("current", 0), ("next", 1), ("old", -1), ("future", 2))
            )
            bad_end = _market_row(profile, "bad-end", 0)
            bad_end["end_date"] = profile.window_start(NOW) + timedelta(
                seconds=profile.duration_seconds + 1
            )
            rows.append(bad_end)
            malformed = _market_row(profile, "malformed", 0)
            malformed["slug"] = str(malformed["slug"]) + "-invalid"
            rows.append(malformed)
        connection.execute(markets.insert(), rows)
        reference_rows = [
            {
                "source": SOURCE,
                "symbol": symbol,
                "price": base + Decimal(second) / 10,
                "source_timestamp": REFERENCE_START + timedelta(seconds=second),
                "observed_at": REFERENCE_START + timedelta(seconds=second),
            }
            for symbol, base in (("BTC/USD", Decimal("100000")), ("ETH/USD", Decimal("2000")))
            for second in range(421)
        ]
        connection.execute(reference_prices.insert(), reference_rows)


def _reader(repository: MarketRepository, profile_id: str) -> IndicatorStateReader:
    return IndicatorStateReader(
        repository,
        settings=Settings(_env_file=None, trading_profile=profile_id, research_context_path=""),
    )


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_snapshot_scopes_market_window_and_reference_symbol(
    repository: MarketRepository, profile_id: str
) -> None:
    profile = get_profile(profile_id)
    snapshot = _reader(repository, profile_id).read(NOW)
    start = profile.window_start(NOW)
    end = start + timedelta(seconds=profile.duration_seconds)
    assert snapshot["status"] == "ok"
    assert snapshot["profile"] == profile.as_dict()
    assert snapshot["market"]["id"] == f"{profile_id}-current"
    assert snapshot["market"]["start"] == start.isoformat()
    assert snapshot["market"]["end"] == end.isoformat()
    assert snapshot["market"]["remaining_seconds"] == (end - NOW).total_seconds()
    assert snapshot["next_market"]["id"] == f"{profile_id}-next"
    assert snapshot["next_market"]["start"] == end.isoformat()
    assert snapshot["chainlink"]["symbol"] == profile.reference_symbol
    base = Decimal("2000") if profile.asset == "ETH" else Decimal("100000")
    assert Decimal(snapshot["chainlink"]["current"]) == base + 42
    assert Decimal(snapshot["chainlink"]["start"]) == (
        base + Decimal(str((start - REFERENCE_START).total_seconds())) / 10
    )
    assert snapshot["model"] is not None
    if profile_id == "polymarket_btc_15m":
        assert snapshot["market"]["start"] == "2026-09-19T12:00:00+00:00"
        assert snapshot["market"]["end"] == "2026-09-19T12:15:00+00:00"
        assert snapshot["market"]["remaining_seconds"] == 480


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_missing_selected_market_waits_instead_of_using_other_profile(
    repository: MarketRepository, profile_id: str
) -> None:
    with repository.engine.begin() as connection:
        connection.execute(markets.delete().where(markets.c.id == f"{profile_id}-current"))
    snapshot = _reader(repository, profile_id).read(NOW)
    assert snapshot == {
        "now": NOW.isoformat(),
        "status": "waiting_for_market",
        "profile": get_profile(profile_id).as_dict(),
        "research": {"status": "disabled", "context": None},
    }


def test_missing_eth_reference_never_falls_back_to_btc(repository: MarketRepository) -> None:
    with repository.engine.begin() as connection:
        connection.execute(reference_prices.delete().where(reference_prices.c.symbol == "ETH/USD"))
    snapshot = _reader(repository, "polymarket_eth_5m").read(NOW)
    assert snapshot["chainlink"]["current"] is None
    assert snapshot["chainlink"]["start"] is None
    assert snapshot["model"] is None
    assert "reference_stale_or_missing" in snapshot["quality"]["issues"]


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_runtime_subscriptions_include_only_selected_current_and_next_windows(
    repository: MarketRepository, profile_id: str
) -> None:
    profile = get_profile(profile_id)
    tokens = repository.list_outcome_tokens(profile=profile, now=NOW)
    assert {(token.market_id, token.outcome, token.token_id) for token in tokens} == {
        (f"{profile_id}-{window}", outcome, f"{profile_id}-{window}-{suffix}")
        for window in ("current", "next")
        for outcome, suffix in (("YES", "up"), ("NO", "down"))
    }
    assert len(tokens) == 4


def test_switching_profiles_keeps_previously_collected_markets_and_references(
    repository: MarketRepository,
) -> None:
    def stored_rows() -> tuple[list[object], list[object]]:
        with repository.engine.connect() as connection:
            return (
                list(connection.execute(select(markets).order_by(markets.c.id))),
                list(connection.execute(select(reference_prices).order_by(reference_prices.c.id))),
            )

    before = stored_rows()
    for profile in list_profiles():
        snapshot = _reader(repository, profile.id).read(NOW)
        assert snapshot["market"]["id"] == f"{profile.id}-current"
        assert len(repository.list_outcome_tokens(profile=profile, now=NOW)) == 4
    assert stored_rows() == before


def _seed_replay(repository: MarketRepository) -> None:
    # Every profile has matching candles and ticks, including BTC15 rows whose
    # question would pass the legacy text filter. Only exact BTC5 contracts count.
    with repository.engine.begin() as connection:
        market_rows = connection.execute(select(markets)).mappings().all()
        replay_times = [NOW.replace(minute=minute) for minute in range(3, 17)]
        for symbol, base in (("BTCUSDT", Decimal("100000")), ("ETHUSDT", Decimal("2000"))):
            for index, at in enumerate(replay_times):
                value = base + index
                connection.execute(
                    price_candles_1m.insert().values(
                        venue="binance", instrument=symbol, bucket=at,
                        open=value, high=value, low=value, close=value, samples=1, updated_at=at,
                    )
                )
        for market in market_rows:
            for token_key, outcome, value in (
                ("yes_token_id", "YES", Decimal("0.45")),
                ("no_token_id", "NO", Decimal("0.53")),
            ):
                for at in replay_times:
                    connection.execute(
                        price_candles_1m.insert().values(
                            venue="polymarket", instrument=market[token_key], bucket=at,
                            open=value, high=value, low=value, close=value,
                            samples=1, updated_at=at,
                        )
                    )
                    connection.execute(
                        price_ticks.insert().values(
                            venue="polymarket", instrument=market[token_key],
                            market_id=market["id"], outcome=outcome,
                            best_bid=value, best_ask=value + Decimal("0.02"), observed_at=at,
                        )
                    )


def test_legacy_replay_loaders_ignore_other_profiles_and_outside_window_events(
    repository: MarketRepository,
) -> None:
    _seed_replay(repository)
    profile = get_profile("polymarket_btc_5m")
    frames = load_signal_replay_frames(repository)
    quotes = load_quote_events(repository)
    expected_ids = {f"{profile.id}-{window}" for window in ("old", "current", "next", "future")}
    assert {frame.market_id for frame in frames} == expected_ids
    assert {quote.market_id for quote in quotes} == expected_ids
    with repository.engine.connect() as connection:
        end_dates = dict(connection.execute(select(markets.c.id, markets.c.end_date)).all())
    for row in [*frames, *quotes]:
        end = end_dates[row.market_id]
        assert end - timedelta(seconds=300) <= row.timestamp < end
    current_id = f"{profile.id}-current"
    current_start = NOW.replace(minute=5)
    current_frame_times = [frame.timestamp for frame in frames if frame.market_id == current_id]
    assert current_frame_times == [current_start + timedelta(minutes=offset) for offset in range(5)]
    current_quotes = [quote for quote in quotes if quote.market_id == current_id]
    assert len(current_quotes) == 10
