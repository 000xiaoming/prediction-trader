from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from prediction_trader.domain.models import Outcome
from prediction_trader.execution_replay import SimulatedFill
from prediction_trader.replay_ledger import build_marked_equity_curve


class ReplayLedgerTests(TestCase):
    def test_builds_equity_and_drawdown(self) -> None:
        now = datetime.now(UTC)

        def fill(offset: int, pnl: str) -> SimulatedFill:
            return SimulatedFill(
                market_id=str(offset),
                outcome=Outcome.YES,
                signal_time=now,
                fill_time=now + timedelta(seconds=offset),
                fill_price=Decimal("0.5"),
                mark_price=Decimal("0.5"),
                notional_usd=Decimal("2"),
                shares=Decimal("4"),
                unrealized_pnl_usd=Decimal(pnl),
            )

        curve = build_marked_equity_curve((fill(2, "-0.75"), fill(1, "1.25"), fill(3, "0.25")))

        self.assertEqual(
            [point.equity_usd for point in curve],
            [Decimal("1.25"), Decimal("0.50"), Decimal("0.75")],
        )
        self.assertEqual(max(point.drawdown_usd for point in curve), Decimal("0.75"))
