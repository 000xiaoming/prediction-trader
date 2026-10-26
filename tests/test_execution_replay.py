from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from prediction_trader.domain.models import Outcome, StrategyGroup, StrategySignal
from prediction_trader.execution_replay import EventExecutionReplay, QuoteEvent


class EventExecutionReplayTests(TestCase):
    def test_uses_first_quote_after_latency(self) -> None:
        now = datetime.now(UTC)
        signal = StrategySignal(
            "test",
            StrategyGroup.CROSS_VENUE,
            "market",
            Decimal("0.7"),
            Decimal("0.8"),
            now,
            now + timedelta(seconds=3),
        )
        quotes = [
            QuoteEvent(
                now + timedelta(milliseconds=100),
                "market",
                Outcome.YES,
                Decimal("0.49"),
                Decimal("0.50"),
            ),
            QuoteEvent(
                now + timedelta(milliseconds=300),
                "market",
                Outcome.YES,
                Decimal("0.50"),
                Decimal("0.51"),
            ),
            QuoteEvent(
                now + timedelta(seconds=1),
                "market",
                Outcome.YES,
                Decimal("0.54"),
                Decimal("0.55"),
            ),
        ]
        result = EventExecutionReplay(
            latency_ms=250,
            max_fill_wait_ms=1000,
            slippage_bps=Decimal("0"),
            notional_usd=Decimal("2"),
        ).run([signal], quotes)

        self.assertEqual(len(result.filled), 1)
        self.assertEqual(result.filled[0].fill_price, Decimal("0.51"))
        self.assertEqual(result.filled[0].mark_price, Decimal("0.54"))

    def test_does_not_fill_from_quote_before_signal(self) -> None:
        now = datetime.now(UTC)
        signal = StrategySignal(
            "test",
            StrategyGroup.CROSS_VENUE,
            "market",
            Decimal("0.7"),
            Decimal("1"),
            now,
            now + timedelta(seconds=2),
        )
        quotes = [
            QuoteEvent(
                now - timedelta(milliseconds=1),
                "market",
                Outcome.YES,
                Decimal("0.4"),
                Decimal("0.41"),
            )
        ]
        result = EventExecutionReplay(
            latency_ms=0,
            max_fill_wait_ms=100,
            slippage_bps=Decimal("0"),
            notional_usd=Decimal("2"),
        ).run([signal], quotes)

        self.assertEqual(len(result.filled), 0)
        self.assertEqual(result.unfilled, 1)
