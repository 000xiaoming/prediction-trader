from datetime import timedelta
from decimal import Decimal

from prediction_trader.domain.models import MarketSnapshot, StrategyGroup, StrategySignal


def _clamp_probability(value: Decimal) -> Decimal:
    return min(Decimal("0.99"), max(Decimal("0.01"), value))


class CrossVenueLeadStrategy:
    name = "binance-polymarket-lead-v1"

    def __init__(self, min_btc_move: Decimal, probability_sensitivity: Decimal) -> None:
        self._min_btc_move = min_btc_move
        self._sensitivity = probability_sensitivity

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal | None:
        question = str(snapshot.metadata.get("question", "")).casefold()
        if "bitcoin up or down" not in question:
            return None
        raw_return = snapshot.metadata.get("binance_return")
        try:
            btc_return = Decimal(str(raw_return))
        except Exception:
            return None
        if abs(btc_return) < self._min_btc_move:
            return None

        market_mid = (snapshot.yes_bid + snapshot.yes_ask) / 2
        fair_probability = _clamp_probability(market_mid + btc_return * self._sensitivity)
        confidence = min(Decimal("1"), abs(btc_return) / (self._min_btc_move * 4))
        return StrategySignal(
            strategy_name=self.name,
            strategy_group=StrategyGroup.CROSS_VENUE,
            market_id=snapshot.market_id,
            yes_probability=fair_probability,
            confidence=confidence,
            observed_at=snapshot.receive_time,
            valid_until=snapshot.receive_time + timedelta(seconds=2),
            evidence={
                "binance_return": str(btc_return),
                "market_mid": str(market_mid),
                "sensitivity": str(self._sensitivity),
                "research_only": True,
            },
        )
