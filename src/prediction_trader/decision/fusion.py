from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from prediction_trader.domain.models import FusedSignal, StrategyGroup, StrategySignal


class SignalFusion:
    def __init__(self, group_weights: Mapping[StrategyGroup, Decimal]) -> None:
        self._weights = group_weights

    def fuse(
        self, market_id: str, signals: Sequence[StrategySignal], now: datetime
    ) -> FusedSignal | None:
        valid = [
            signal
            for signal in signals
            if signal.market_id == market_id
            and signal.observed_at <= now <= signal.valid_until
            and Decimal("0") <= signal.yes_probability <= Decimal("1")
            and Decimal("0") < signal.confidence <= Decimal("1")
        ]
        if not valid:
            return None

        weighted: list[tuple[StrategySignal, Decimal]] = []
        for signal in valid:
            group_weight = self._weights.get(signal.strategy_group, Decimal("0"))
            effective_weight = group_weight * signal.confidence
            if effective_weight > 0:
                weighted.append((signal, effective_weight))
        if not weighted:
            return None

        denominator = sum((weight for _, weight in weighted), Decimal("0"))
        probability = sum(
            (signal.yes_probability * weight for signal, weight in weighted), Decimal("0")
        ) / denominator
        confidence = min(Decimal("1"), denominator / Decimal(len(weighted)))
        return FusedSignal(market_id, probability, confidence, now, len(weighted))
