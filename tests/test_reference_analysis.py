from datetime import UTC, datetime, timedelta
from decimal import Decimal

from prediction_trader.reference_analysis import ReferencePoint, analyze_reference

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
END = START + timedelta(minutes=5)
NOW = START + timedelta(seconds=30)


def points() -> list[ReferencePoint]:
    return [
        ReferencePoint(
            START + timedelta(seconds=i),
            START + timedelta(seconds=i),
            Decimal("60000") + Decimal(i % 3),
        )
        for i in range(31)
    ]


def test_complete_fresh_history_produces_experimental_estimate() -> None:
    result = analyze_reference(points(), start=START, end=END, now=NOW)
    assert result.estimate is not None
    assert result.opening == points()[0]
    assert result.issues == ()


def test_mid_window_start_is_not_a_fake_opening_price() -> None:
    result = analyze_reference(points()[10:], start=START, end=END, now=NOW)
    assert result.opening is None
    assert result.estimate is None
    assert "opening_reference_missing" in result.issues


def test_stale_and_expired_windows_suppress_probability() -> None:
    stale = analyze_reference(points(), start=START, end=END, now=NOW + timedelta(seconds=6))
    expired = analyze_reference(points(), start=START, end=END, now=END)
    assert stale.estimate is None
    assert "reference_stale_or_missing" in stale.issues
    assert expired.estimate is None
    assert "outside_market_window" in expired.issues


def test_event_and_receive_time_both_bound_information_availability() -> None:
    observations = points()
    observations.extend(
        [
            ReferencePoint(NOW + timedelta(seconds=1), NOW, Decimal("999999")),
            ReferencePoint(NOW, NOW + timedelta(seconds=1), Decimal("888888")),
        ]
    )
    result = analyze_reference(observations, start=START, end=END, now=NOW)
    assert result.current == points()[-1]
    assert result.sample_count == 31


def test_reconnect_duplicates_cannot_create_sample_coverage() -> None:
    observations = [points()[0]] * 30 + [points()[-1]] * 30
    result = analyze_reference(observations, start=START, end=END, now=NOW)
    assert result.sample_count == 2
    assert result.estimate is None
    assert "insufficient_reference_history" in result.issues
    assert "reference_history_gap" in result.issues


def test_nonfinite_negative_and_zero_prices_are_ignored() -> None:
    observations = points() + [
        ReferencePoint(NOW, NOW, Decimal(value)) for value in ("NaN", "Infinity", "-1", "0")
    ]
    result = analyze_reference(observations, start=START, end=END, now=NOW)
    assert result.current == points()[-1]
    assert result.estimate is not None


def test_empty_history_returns_quality_issues() -> None:
    result = analyze_reference([], start=START, end=END, now=NOW)
    assert result.estimate is None
    assert result.current is None
    assert "insufficient_reference_history" in result.issues
