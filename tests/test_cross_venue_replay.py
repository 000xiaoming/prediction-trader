from datetime import UTC, datetime
from decimal import Decimal
from unittest import TestCase

from prediction_trader.replay import HistoricalReplay, ReplayFrame
from prediction_trader.strategies.cross_venue_lead import CrossVenueLeadStrategy


class CrossVenueReplayTests(TestCase):
    def test_positive_btc_move_emits_yes_leaning_signal(self) -> None:
        frame = ReplayFrame(
            timestamp=datetime.now(UTC),
            market_id="btc-updown",
            question="Bitcoin Up or Down - August 23, 10:00AM ET",
            yes_bid=Decimal("0.49"),
            yes_ask=Decimal("0.51"),
            no_bid=Decimal("0.49"),
            no_ask=Decimal("0.51"),
            binance_price=Decimal("100.20"),
            binance_price_lookback=Decimal("100"),
            seconds_to_expiry=300,
        )
        strategy = CrossVenueLeadStrategy(Decimal("0.0005"), Decimal("25"))

        result = HistoricalReplay(strategy).run([frame])

        self.assertEqual(result.frames, 1)
        self.assertEqual(len(result.signals), 1)
        self.assertGreater(result.signals[0].yes_probability, Decimal("0.5"))

    def test_refuses_semantically_different_bitcoin_market(self) -> None:
        frame = ReplayFrame(
            datetime.now(UTC),
            "btc-150k",
            "Will Bitcoin hit $150k by December 31?",
            Decimal("0.2"),
            Decimal("0.21"),
            Decimal("0.79"),
            Decimal("0.8"),
            Decimal("101"),
            Decimal("100"),
            100000,
        )
        strategy = CrossVenueLeadStrategy(Decimal("0.0005"), Decimal("25"))

        self.assertEqual(HistoricalReplay(strategy).run([frame]).signals, ())
