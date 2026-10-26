from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from prediction_trader.indicators import (
    calculate_manual_trade,
    estimate_remaining_probability,
)


class IndicatorTests(TestCase):
    def test_manual_trade_includes_both_taker_fees(self) -> None:
        result = calculate_manual_trade(
            stake_usd=Decimal("1"),
            buy_price=Decimal("0.50"),
            sell_price=Decimal("0.60"),
            fee_rate=Decimal("0.07"),
            target_profit_usd=Decimal("0.05"),
        )

        self.assertEqual(result.total_cost_usd.quantize(Decimal("0.0001")), Decimal("1.0000"))
        self.assertGreater(result.exit_pnl_usd, 0)
        self.assertGreater(result.target_sell_price or Decimal("0"), Decimal("0.50"))

    def test_probability_moves_above_half_when_price_is_above_start(self) -> None:
        now = datetime.now(UTC)
        prices = [
            (now + timedelta(seconds=index), Decimal("100") + Decimal(index) / 10)
            for index in range(20)
        ]
        result = estimate_remaining_probability(
            prices, start_price=Decimal("100"), remaining_seconds=30
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreater(result.up_probability, Decimal("0.5"))
