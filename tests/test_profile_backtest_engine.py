import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from prediction_trader.backtesting.engine import (
    BacktestParameters,
    HistoricalMarket,
    HistoricalQuote,
    HistoricalReference,
    HistoricalSettlement,
    explicit_fee_rate,
    replay_profile,
)
from prediction_trader.profiles import MarketProfile, get_profile, list_profiles

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
SOURCE = "polymarket_rtds_chainlink_twap_60"
BTC = get_profile("polymarket_btc_5m")


def _parameters(profile: MarketProfile = BTC, **changes: object) -> BacktestParameters:
    params = BacktestParameters(
        start=START,
        end=START + timedelta(seconds=profile.duration_seconds),
        as_of=START + timedelta(seconds=profile.duration_seconds + 120),
        slippage_bps=Decimal("0"),
    )
    return replace(params, **changes)


def _quote(second: float, ask: str = "0.40", outcome: str = "YES") -> HistoricalQuote:
    at = START + timedelta(seconds=second)
    return HistoricalQuote(
        token_id="up" if outcome == "YES" else "down",
        outcome=outcome,
        observed_at=at,
        source_time=at,
        best_ask=Decimal(ask),
        sequence=int(second * 1000),
    )


def _market(profile: MarketProfile = BTC, **changes: object) -> HistoricalMarket:
    end = START + timedelta(seconds=profile.duration_seconds)
    base = Decimal("100000") if profile.asset == "BTC" else Decimal("2000")
    market = HistoricalMarket(
        market_id=profile.id,
        slug=profile.slug_at(START),
        end=end,
        yes_token_id="up",
        no_token_id="down",
        metadata_observed_at=START,
        raw={"feeSchedule": {"rate": "0.07", "exponent": 1}},
        references=tuple(
            HistoricalReference(
                source=SOURCE,
                symbol=profile.reference_symbol,
                source_time=START + timedelta(seconds=second),
                observed_at=START + timedelta(seconds=second),
                price=base + Decimal(second) / 10 + Decimal(second % 2) / 100,
            )
            for second in range(61)
        ),
        quotes=(_quote(59), _quote(59, "0.60", "NO"), _quote(61)),
        settlement=HistoricalSettlement(
            "YES",
            end + timedelta(seconds=10),
            end + timedelta(seconds=20),
        ),
    )
    return replace(market, **changes)


@pytest.mark.parametrize("profile", list_profiles(), ids=lambda profile: profile.id)
def test_profiles_produce_independent_fee_inclusive_results(profile: MarketProfile) -> None:
    market = _market(profile)
    report = replay_profile(profile, _parameters(profile), [market])
    metrics = report["metrics"]
    assert report["profile_id"] == profile.id
    assert metrics["prediction_count"] == metrics["fill_count"] == 1
    fill = report["decisions"][0]["fill"]
    price = Decimal(fill["price"])
    fee = Decimal(fill["fee_usd"])
    shares = Decimal(fill["shares"])
    assert price == Decimal("0.4")
    assert fee == shares * Decimal("0.07") * price * (1 - price)
    assert shares * price + fee == pytest.approx(Decimal("10"))
    assert Decimal(metrics["realized_pnl_usd"]) == shares - 10
    assert Decimal(metrics["brier_score"]) == Decimal("0.0001")
    assert sum(row["count"] for row in report["calibration_bins"]) == 1
    json.dumps(report)
    assert report == replay_profile(profile, _parameters(profile), [market])


def test_profile_and_duration_are_both_required() -> None:
    other = _market(get_profile("polymarket_eth_5m"))
    bad = _market(market_id="wrong-duration", end=START + timedelta(minutes=15))
    malformed = _market(market_id="bad-slug", slug=BTC.slug_at(START) + "-junk")
    result = replay_profile(BTC, _parameters(), [other, bad, malformed, _market()])
    assert result["metrics"]["market_count"] == 1
    assert result["decisions"][0]["market_id"] == BTC.id


@pytest.mark.parametrize(
    "failure",
    ["asset", "source", "late_open", "future_source", "sparse", "stale", "gap", "missing_open"],
)
def test_reference_quality_and_time_filters_suppress_forecasts(failure: str) -> None:
    market = _market()
    refs = market.references
    if failure == "asset":
        refs = tuple(replace(ref, symbol="ETH/USD") for ref in refs)
    elif failure == "source":
        refs = tuple(replace(ref, source="polymarket_rtds_chainlink") for ref in refs)
    elif failure == "late_open":
        refs = tuple(
            replace(ref, observed_at=START + timedelta(seconds=61))
            if ref.source_time <= START + timedelta(seconds=2)
            else ref
            for ref in refs
        )
    elif failure == "future_source":
        refs = tuple(
            replace(ref, source_time=ref.source_time + timedelta(minutes=1)) for ref in refs
        )
    elif failure == "sparse":
        refs = refs[::15]
    elif failure == "stale":
        refs = refs[:50]
    elif failure == "gap":
        refs = refs[:20] + refs[40:]
    elif failure == "missing_open":
        refs = refs[3:]
    report = replay_profile(BTC, _parameters(), [replace(market, references=refs)])
    assert report["metrics"]["prediction_count"] == report["metrics"]["fill_count"] == 0
    assert report["decisions"][0]["issues"]


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"feeSchedule": {"rate": "NaN"}},
        {"feeSchedule": {"rate": -1}},
        {"feeSchedule": {"rate": 0.2, "exponent": 2}},
    ],
)
def test_missing_or_unsupported_fees_keep_forecast_but_skip_trade(raw: dict[str, object]) -> None:
    report = replay_profile(BTC, _parameters(), [_market(raw=raw)])
    assert report["metrics"]["prediction_count"] == 1
    assert report["metrics"]["fill_count"] == 0
    assert report["metrics"]["brier_score"] is not None
    assert report["decisions"][0]["issues"] == ["fee_schedule_missing_or_unsupported"]


def test_explicit_zero_fees_are_supported_without_fallback() -> None:
    assert explicit_fee_rate({"feesEnabled": False}) == 0
    report = replay_profile(BTC, _parameters(), [_market(raw={"feesEnabled": False})])
    assert Decimal(report["metrics"]["total_fees_usd"]) == 0


def test_fee_metadata_cannot_be_backdated_but_historical_snapshot_can_be_used() -> None:
    market = _market(metadata_observed_at=START + timedelta(minutes=3))
    report = replay_profile(BTC, _parameters(), [market])
    assert report["metrics"]["fill_count"] == 0
    assert report["decisions"][0]["issues"] == ["fee_not_known_at_decision"]
    snapshot = replace(market, fee_raw={"feesEnabled": False}, fee_observed_at=START)
    assert replay_profile(BTC, _parameters(), [snapshot])["metrics"]["fill_count"] == 1


def test_fee_adjusted_edge_blocks_gross_profitable_bet() -> None:
    market = _market(
        raw={"feeSchedule": {"rate": "1"}},
        quotes=(_quote(59, "0.90"), _quote(61, "0.90")),
    )
    assert replay_profile(BTC, _parameters(), [market])["metrics"]["signal_count"] == 0


@pytest.mark.parametrize(
    "future_quotes",
    [
        (),
        (_quote(60),),
        (_quote(60.1),),
        (_quote(66),),
        (replace(_quote(61), source_time=START),),
        (replace(_quote(61), source_time=START + timedelta(seconds=70)),),
    ],
)
def test_fill_requires_a_new_timely_quote_after_latency(
    future_quotes: tuple[HistoricalQuote, ...],
) -> None:
    report = replay_profile(BTC, _parameters(), [_market(quotes=(_quote(59),) + future_quotes)])
    assert report["metrics"]["signal_count"] == 1
    assert report["metrics"]["fill_count"] == 0
    assert report["metrics"]["unfilled_count"] == 1


def test_zero_latency_still_cannot_fill_on_signal_quote() -> None:
    report = replay_profile(BTC, _parameters(latency_ms=0), [_market(quotes=(_quote(60),))])
    assert report["metrics"]["signal_count"] == 1
    assert report["metrics"]["fill_count"] == 0


def test_new_empty_book_invalidates_previously_valid_ask() -> None:
    quotes = (_quote(58), replace(_quote(59), best_ask=None), _quote(61))
    report = replay_profile(BTC, _parameters(), [_market(quotes=quotes)])
    assert report["metrics"]["signal_count"] == 0


def test_first_eligible_quote_wins_and_respects_slippage_limit() -> None:
    quotes = (
        _quote(59),
        _quote(60.1),
        _quote(60.5, "0.42"),
        _quote(61, "0.4001"),
        _quote(62, "0.39"),
    )
    report = replay_profile(BTC, _parameters(slippage_bps=Decimal("5")), [_market(quotes=quotes)])
    fill = report["decisions"][0]["fill"]
    assert fill["fill_time"] == (START + timedelta(seconds=61)).isoformat()
    assert Decimal(fill["price"]) == Decimal("0.40020")


def test_quote_from_wrong_token_cannot_signal_or_fill() -> None:
    quotes = tuple(replace(quote, token_id="unrelated") for quote in _market().quotes)
    report = replay_profile(BTC, _parameters(), [_market(quotes=quotes)])
    assert report["metrics"]["signal_count"] == 0


def test_unresolved_and_not_yet_observed_settlements_have_no_profit() -> None:
    market = _market()
    assert market.settlement is not None
    for settlement in (
        None,
        replace(market.settlement, observed_at=START + timedelta(days=1)),
        replace(market.settlement, resolved_at=START + timedelta(days=1)),
    ):
        report = replay_profile(BTC, _parameters(), [replace(market, settlement=settlement)])
        assert report["metrics"]["fill_count"] == 1
        assert report["metrics"]["unresolved_fill_count"] == 1
        assert report["metrics"]["resolved_prediction_count"] == 0
        assert report["metrics"]["realized_pnl_usd"] == "0"
        assert report["metrics"]["unresolved_cost_usd"] == "10"
        assert report["metrics"]["brier_score"] is None
        assert report["decisions"][0]["fill"]["realized_pnl_usd"] is None


def test_loss_includes_fee_and_drives_realized_drawdown() -> None:
    market = _market()
    assert market.settlement is not None
    market = replace(market, settlement=replace(market.settlement, winning_outcome="NO"))
    report = replay_profile(BTC, _parameters(), [market])
    assert report["metrics"]["realized_pnl_usd"] == "-10"
    assert report["metrics"]["max_realized_drawdown_usd"] == "10"
    assert Decimal(report["metrics"]["total_fees_usd"]) > 0


def test_as_of_prevents_future_fills_and_marks_pending_window() -> None:
    params = _parameters(end=START + timedelta(seconds=60), as_of=START + timedelta(seconds=60.5))
    report = replay_profile(BTC, params, [_market()])
    assert report["metrics"]["fill_count"] == 0
    assert report["metrics"]["pending_order_count"] == 1
    assert report["metrics"]["unfilled_count"] == 0


def test_no_fill_at_or_after_market_expiry() -> None:
    params = _parameters(timeout_seconds=300)
    market = _market(quotes=(_quote(59), _quote(300), _quote(301)))
    report = replay_profile(BTC, params, [market])
    assert report["metrics"]["signal_count"] == 1
    assert report["metrics"]["unfilled_count"] == 1
    assert report["metrics"]["fill_count"] == 0


def test_fifteen_minute_profile_accepts_later_decision_with_opening_history() -> None:
    profile = get_profile("polymarket_btc_15m")
    market = _market(profile)
    additional = tuple(
        replace(
            market.references[-1],
            source_time=START + timedelta(seconds=second),
            observed_at=START + timedelta(seconds=second),
            price=Decimal("100000") + Decimal(second) / 10,
        )
        for second in range(240, 301)
    )
    market = replace(
        market, references=market.references + additional, quotes=(_quote(299), _quote(301))
    )
    report = replay_profile(profile, _parameters(profile, decision_delay_seconds=300), [market])
    assert report["metrics"]["prediction_count"] == report["metrics"]["fill_count"] == 1
    assert report["decisions"][0]["decision_at"] == (START + timedelta(minutes=5)).isoformat()


def test_contract_twap_mismatch_suppresses_probability() -> None:
    market = _market(raw={"cryptoMarketConfig": {"twapLookbackSeconds": 30}})
    report = replay_profile(BTC, _parameters(), [market])
    assert report["metrics"]["prediction_count"] == 0
    assert report["decisions"][0]["issues"] == ["contract_twap_window_mismatch"]


def test_event_limit_omissions_never_become_valid_forecasts() -> None:
    report = replay_profile(BTC, _parameters(), [_market(data_issues=("event_limit_exceeded",))])
    assert report["metrics"]["prediction_count"] == 0


def test_market_cap_is_explicit_and_duplicate_market_is_not_double_counted() -> None:
    market = _market()
    other = _market(market_id="other")
    report = replay_profile(BTC, _parameters(max_markets=1), [market, market, other])
    assert report["truncated"] is True
    assert report["metrics"]["market_count"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"start": START.replace(tzinfo=None)},
        {"end": START},
        {"start": START - timedelta(days=8)},
        {"max_markets": 5001},
        {"slippage_bps": Decimal("NaN")},
        {"min_edge": Decimal("-0.1")},
        {"stake_usd": Decimal("0")},
        {"latency_ms": -1},
        {"twap_window_seconds": 5},
    ],
)
def test_invalid_parameters_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _parameters(**changes)


def test_delay_must_fit_selected_profile() -> None:
    params = _parameters(decision_delay_seconds=300)
    with pytest.raises(ValueError, match="shorter"):
        replay_profile(BTC, params, [])
    params.validate_profile(get_profile("polymarket_btc_15m"))
