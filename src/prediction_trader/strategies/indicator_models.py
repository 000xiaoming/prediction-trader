"""Indicator-model boundary, separate from research ratings and order execution."""

from collections.abc import Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from prediction_trader.reference_analysis import (
    ReferenceAnalysis,
    ReferencePoint,
    analyze_reference,
)


class IndicatorModel(Protocol):
    def analyze(
        self,
        points: Sequence[ReferencePoint],
        *,
        start: datetime,
        end: datetime,
        now: datetime,
        max_age_seconds: float,
        opening_tolerance_seconds: float,
    ) -> ReferenceAnalysis: ...


class RemainingTimeModel:
    """Experimental price-distance model; no calibration claim for any profile."""

    def analyze(
        self,
        points: Sequence[ReferencePoint],
        *,
        start: datetime,
        end: datetime,
        now: datetime,
        max_age_seconds: float,
        opening_tolerance_seconds: float,
    ) -> ReferenceAnalysis:
        return analyze_reference(
            points,
            start=start,
            end=end,
            now=now,
            max_age_seconds=max_age_seconds,
            opening_tolerance_seconds=opening_tolerance_seconds,
        )


_MODELS: MappingProxyType[str, IndicatorModel] = MappingProxyType(
    {
        "remaining_time_v1": RemainingTimeModel(),
    }
)


def get_indicator_model(strategy_id: str) -> IndicatorModel:
    try:
        return _MODELS[strategy_id]
    except KeyError:
        raise ValueError(f"Unknown indicator model; available: {', '.join(_MODELS)}") from None
