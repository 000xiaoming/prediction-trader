from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from prediction_trader.adapters.paper import PaperExecutionAdapter
from prediction_trader.decision.engine import DecisionEngine
from prediction_trader.decision.fusion import SignalFusion
from prediction_trader.domain.models import (
    MarketSnapshot,
    OrderIntent,
    Outcome,
    StrategyGroup,
    StrategySignal,
    TargetPosition,
)
from prediction_trader.execution.manager import OrderManager
from prediction_trader.execution.planner import ExecutionPlanner
from prediction_trader.risk.engine import RiskEngine


class DecisionPathTest(TestCase):
    def test_signal_to_idempotent_paper_order(self) -> None:
        now = datetime.now(UTC)
        snapshot = MarketSnapshot(
            market_id="btc-up-test",
            event_time=now,
            receive_time=now,
            yes_bid=Decimal("0.55"),
            yes_ask=Decimal("0.56"),
            no_bid=Decimal("0.43"),
            no_ask=Decimal("0.44"),
            seconds_to_expiry=300,
        )
        signals = [
            StrategySignal(
                "cross-venue-v1",
                StrategyGroup.CROSS_VENUE,
                snapshot.market_id,
                Decimal("0.64"),
                Decimal("0.8"),
                now,
                now + timedelta(seconds=5),
            ),
            StrategySignal(
                "book-v1",
                StrategyGroup.MICROSTRUCTURE,
                snapshot.market_id,
                Decimal("0.62"),
                Decimal("0.7"),
                now,
                now + timedelta(seconds=5),
            ),
        ]
        fusion = SignalFusion(
            {
                StrategyGroup.CROSS_VENUE: Decimal("0.6"),
                StrategyGroup.MICROSTRUCTURE: Decimal("0.4"),
            }
        )
        fused = fusion.fuse(snapshot.market_id, signals, now)
        self.assertIsNotNone(fused)
        assert fused is not None

        target = DecisionEngine(Decimal("0.02"), Decimal("2")).decide(
            snapshot, fused, Decimal("0.01"), now
        )
        self.assertIsNotNone(target)
        assert target is not None

        risk = RiskEngine(Decimal("10"), Decimal("25"), Decimal("10")).assess(
            target,
            now=now,
            market_exposure_usd=Decimal("0"),
            total_exposure_usd=Decimal("0"),
            daily_pnl_usd=Decimal("0"),
            reconciled=True,
        )
        plan = ExecutionPlanner().plan(target, risk, snapshot, now)
        intent = OrderIntent(plan)
        manager = OrderManager(PaperExecutionAdapter())

        first = manager.submit(intent)
        second = manager.submit(intent)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("paper-"))

    def test_unreconciled_account_is_fail_closed(self) -> None:
        now = datetime.now(UTC)
        target = TargetPosition(
            "market",
            Outcome.YES,
            Decimal("2"),
            Decimal("0.5"),
            now + timedelta(seconds=2),
            "test",
        )
        decision = RiskEngine(Decimal("10"), Decimal("25"), Decimal("10")).assess(
            target,
            now=now,
            market_exposure_usd=Decimal("0"),
            total_exposure_usd=Decimal("0"),
            daily_pnl_usd=Decimal("0"),
            reconciled=False,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "account_not_reconciled")
