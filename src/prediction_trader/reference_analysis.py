"""Pure, point-in-time reference analysis shared by the dashboard and offline tests."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from prediction_trader.indicators import ProbabilityEstimate, estimate_remaining_probability


@dataclass(frozen=True, slots=True)
class ReferencePoint:
    source_time: datetime
    observed_at: datetime
    price: Decimal


@dataclass(frozen=True, slots=True)
class ReferenceAnalysis:
    current: ReferencePoint | None
    opening: ReferencePoint | None
    estimate: ProbabilityEstimate | None
    issues: tuple[str, ...]
    sample_count: int


def analyze_reference(
    points: Sequence[ReferencePoint],
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    max_age_seconds: float = 5,
    opening_tolerance_seconds: float = 2,
) -> ReferenceAnalysis:
    """Suppress a model with missing opening, stale, sparse or interrupted observations.

    The opening point is a local approximation, not a verified settlement price.
    Both event time and receipt time must be known by ``now``; duplicate event
    timestamps cannot inflate the volatility sample count.
    """
    unique = {
        point.source_time: point
        for point in sorted(points, key=lambda point: point.observed_at)
        if point.source_time <= now
        and point.observed_at <= now
        and point.price.is_finite()
        and point.price > 0
    }
    ordered = sorted(unique.values(), key=lambda point: point.source_time)
    current = ordered[-1] if ordered else None
    opening = next(
        (
            point
            for point in ordered
            if start <= point.source_time <= start + timedelta(seconds=opening_tolerance_seconds)
        ),
        None,
    )
    recent = [point for point in ordered if point.source_time >= now - timedelta(seconds=60)]
    issues: list[str] = []
    if not start <= now < end:
        issues.append("outside_market_window")
    if opening is None:
        issues.append("opening_reference_missing")
    if current is None or (now - current.source_time).total_seconds() > max_age_seconds:
        issues.append("reference_stale_or_missing")
    if len(recent) < 10 or (recent[-1].source_time - recent[0].source_time).total_seconds() < 10:
        issues.append("insufficient_reference_history")
    if any(
        (later.source_time - earlier.source_time).total_seconds() > 10
        for earlier, later in zip(recent, recent[1:], strict=False)
    ):
        issues.append("reference_history_gap")
    estimate = None
    if not issues and opening is not None:
        estimate = estimate_remaining_probability(
            [(point.source_time, point.price) for point in recent],
            start_price=opening.price,
            remaining_seconds=max(0, int((end - now).total_seconds())),
        )
    return ReferenceAnalysis(current, opening, estimate, tuple(issues), len(recent))
