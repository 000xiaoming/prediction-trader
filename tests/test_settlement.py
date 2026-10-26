from decimal import Decimal
from unittest import TestCase

from prediction_trader.domain.models import Outcome
from prediction_trader.settlement import parse_gamma_settlement


class SettlementParsingTests(TestCase):
    def test_parses_closed_binary_winner(self) -> None:
        result = parse_gamma_settlement(
            {
                "id": "123",
                "closed": True,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["1", "0"]',
                "closedTime": "2026-08-23T03:00:00Z",
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.winning_outcome, Outcome.YES)

    def test_rejects_closed_but_unresolved_prices(self) -> None:
        result = parse_gamma_settlement(
            {
                "id": "123",
                "closed": True,
                "outcomes": ["Yes", "No"],
                "outcomePrices": [Decimal("0.55"), Decimal("0.45")],
            }
        )

        self.assertIsNone(result)

    def test_maps_up_down_and_yes_no_by_label_in_either_order(self) -> None:
        for labels in (["Up", "Down"], ["Down", "Up"], ["Yes", "No"], ["No", "Yes"]):
            for prices in (["1", "0"], ["0", "1"]):
                with self.subTest(labels=labels, prices=prices):
                    result = parse_gamma_settlement(
                        {"id": "123", "closed": True, "outcomes": labels, "outcomePrices": prices}
                    )
                    self.assertIsNotNone(result)
                    assert result is not None
                    winner = labels[prices.index("1")]
                    expected = Outcome.YES if winner in {"Up", "Yes"} else Outcome.NO
                    self.assertEqual(result.winning_outcome, expected)

    def test_parses_serialized_up_down_with_normalized_labels(self) -> None:
        result = parse_gamma_settlement(
            {
                "id": "123",
                "closed": True,
                "outcomes": '[" down ", "UP"]',
                "outcomePrices": '["1.0", "0.0"]',
            }
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.winning_outcome, Outcome.NO)

    def test_rejects_unrecognized_duplicate_or_nonbinary_outcomes(self) -> None:
        for outcomes in (
            ["Up", "Up"],
            ["Yes", "Yes"],
            ["Up", "No"],
            ["Yes", "Unknown"],
            ["Up", "Unknown"],
            ["A", "B"],
            [None, "Down"],
            ["Up"],
            ["Up", "Down", "Other"],
            [],
        ):
            with self.subTest(outcomes=outcomes):
                result = parse_gamma_settlement(
                    {
                        "id": "123",
                        "closed": True,
                        "outcomes": outcomes,
                        "outcomePrices": ["1", "0"],
                    }
                )
                self.assertIsNone(result)

    def test_requires_exact_unambiguous_binary_payouts(self) -> None:
        for prices in (
            ["1", "1"],
            ["0", "0"],
            ["1", "0.5"],
            ["1", "-1"],
            ["2", "0"],
            ["1", "NaN"],
            ["1", "sNaN"],
            ["Infinity", "0"],
            ["1", None],
            ["1", True],
            ["1"],
            ["1", "0", "0"],
        ):
            with self.subTest(prices=prices):
                result = parse_gamma_settlement(
                    {
                        "id": "123",
                        "closed": True,
                        "outcomes": ["Up", "Down"],
                        "outcomePrices": prices,
                    }
                )
                self.assertIsNone(result)
