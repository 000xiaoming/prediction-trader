from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from prediction_trader.domain.models import Outcome, StrategySignal


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    timestamp: datetime
    market_id: str
    outcome: Outcome
    bid: Decimal | None
    ask: Decimal | None


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    market_id: str
    outcome: Outcome
    signal_time: datetime
    fill_time: datetime
    fill_price: Decimal
    mark_price: Decimal | None
    notional_usd: Decimal
    shares: Decimal
    unrealized_pnl_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class ExecutionReplayResult:
    signals: int
    filled: tuple[SimulatedFill, ...]
    unfilled: int

    @property
    def marked_pnl_usd(self) -> Decimal:
        return sum(
            (fill.unrealized_pnl_usd or Decimal("0") for fill in self.filled),
            Decimal("0"),
        )


class EventExecutionReplay:
    def __init__(
        self,
        *,
        latency_ms: int,
        max_fill_wait_ms: int,
        slippage_bps: Decimal,
        notional_usd: Decimal,
    ) -> None:
        self._latency = timedelta(milliseconds=latency_ms)
        self._max_wait = timedelta(milliseconds=max_fill_wait_ms)
        self._slippage = slippage_bps / Decimal("10000")
        self._notional = notional_usd

    def run(
        self, signals: list[StrategySignal], quotes: list[QuoteEvent]
    ) -> ExecutionReplayResult:
        grouped: dict[tuple[str, Outcome], list[QuoteEvent]] = {}
        for quote in sorted(quotes, key=lambda item: item.timestamp):
            grouped.setdefault((quote.market_id, quote.outcome), []).append(quote)

        fills: list[SimulatedFill] = []
        unfilled = 0
        for signal in signals:
            outcome = Outcome.YES if signal.yes_probability >= Decimal("0.5") else Outcome.NO
            stream = grouped.get((signal.market_id, outcome), [])
            eligible_at = signal.observed_at + self._latency
            timestamps = [quote.timestamp for quote in stream]
            index = bisect_left(timestamps, eligible_at)
            fill_quote = self._first_ask(stream, index, eligible_at + self._max_wait)
            if fill_quote is None or fill_quote.ask is None:
                unfilled += 1
                continue
            fill_price = min(Decimal("0.99"), fill_quote.ask * (1 + self._slippage))
            shares = self._notional / fill_price
            mark_price = self._latest_bid(stream)
            marked_pnl = None
            if mark_price is not None:
                marked_pnl = shares * (mark_price - fill_price)
            fills.append(
                SimulatedFill(
                    market_id=signal.market_id,
                    outcome=outcome,
                    signal_time=signal.observed_at,
                    fill_time=fill_quote.timestamp,
                    fill_price=fill_price,
                    mark_price=mark_price,
                    notional_usd=self._notional,
                    shares=shares,
                    unrealized_pnl_usd=marked_pnl,
                )
            )
        return ExecutionReplayResult(len(signals), tuple(fills), unfilled)

    @staticmethod
    def _first_ask(
        stream: list[QuoteEvent], start: int, deadline: datetime
    ) -> QuoteEvent | None:
        for quote in stream[start:]:
            if quote.timestamp > deadline:
                return None
            if quote.ask is not None:
                return quote
        return None

    @staticmethod
    def _latest_bid(stream: list[QuoteEvent]) -> Decimal | None:
        for quote in reversed(stream):
            if quote.bid is not None:
                return quote.bid
        return None
