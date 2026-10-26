from typing import Protocol

from prediction_trader.domain.models import MarketSnapshot, StrategySignal


class Strategy(Protocol):
    name: str

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal | None: ...

