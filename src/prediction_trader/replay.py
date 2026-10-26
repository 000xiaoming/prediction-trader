from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from prediction_trader.domain.models import MarketSnapshot, StrategySignal
from prediction_trader.strategies.base import Strategy


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    timestamp: datetime
    market_id: str
    question: str
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    binance_price: Decimal
    binance_price_lookback: Decimal
    seconds_to_expiry: int


@dataclass(frozen=True, slots=True)
class ReplayResult:
    frames: int
    signals: tuple[StrategySignal, ...]


class HistoricalReplay:
    def __init__(self, strategy: Strategy) -> None:
        self._strategy = strategy

    def run(self, frames: Iterable[ReplayFrame]) -> ReplayResult:
        signals: list[StrategySignal] = []
        count = 0
        for frame in frames:
            count += 1
            snapshot = self._snapshot(frame)
            signal = self._strategy.evaluate(snapshot)
            if signal is not None:
                signals.append(signal)
        return ReplayResult(count, tuple(signals))

    def iter_signals(self, frames: Iterable[ReplayFrame]) -> Iterator[StrategySignal]:
        for frame in frames:
            signal = self._strategy.evaluate(self._snapshot(frame))
            if signal is not None:
                yield signal

    @staticmethod
    def _snapshot(frame: ReplayFrame) -> MarketSnapshot:
        btc_return = frame.binance_price / frame.binance_price_lookback - 1
        return MarketSnapshot(
            market_id=frame.market_id,
            event_time=frame.timestamp,
            receive_time=frame.timestamp,
            yes_bid=frame.yes_bid,
            yes_ask=frame.yes_ask,
            no_bid=frame.no_bid,
            no_ask=frame.no_ask,
            seconds_to_expiry=frame.seconds_to_expiry,
            metadata={"question": frame.question, "binance_return": str(btc_return)},
        )
