import unittest
from datetime import UTC, datetime, timedelta

from prediction_trader.adapters.polymarket import parse_events
from prediction_trader.profiles import get_profile


class ParseEventsTests(unittest.TestCase):
    def test_parses_matching_market_and_tokens(self) -> None:
        payload = {
            "events": [
                {
                    "id": "event-1",
                    "slug": "bitcoin-up-or-down",
                    "title": "Bitcoin Up or Down",
                    "markets": [
                        {
                            "id": "market-1",
                            "slug": "bitcoin-up",
                            "question": "Will Bitcoin go up?",
                            "conditionId": "0xabc",
                            "clobTokenIds": '["yes-token", "no-token"]',
                            "endDate": "2026-08-23T12:00:00Z",
                        }
                    ],
                }
            ]
        }

        result = parse_events(payload, market_filter="bitcoin")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].yes_token_id, "yes-token")
        self.assertEqual(result[0].no_token_id, "no-token")

    def test_skips_non_matching_and_non_clob_markets(self) -> None:
        payload = [
            {
                "id": "event-1",
                "title": "Election",
                "markets": [{"id": "market-1", "clobTokenIds": "[]"}],
            }
        ]

        self.assertEqual(parse_events(payload, market_filter="bitcoin"), [])

    def test_maps_reversed_binary_outcomes(self) -> None:
        for outcomes in ('["Down", "Up"]', ["No", "Yes"]):
            with self.subTest(outcomes=outcomes):
                payload = [
                    {
                        "id": "event",
                        "markets": [
                            {
                                "id": "market",
                                "outcomes": outcomes,
                                "clobTokenIds": ["negative", "positive"],
                            }
                        ],
                    }
                ]
                found = parse_events(payload)
                self.assertEqual(found[0].yes_token_id, "positive")
                self.assertEqual(found[0].no_token_id, "negative")

    def test_scoped_parser_requires_verified_up_down_market(self) -> None:
        profile = get_profile("polymarket_eth_5m")
        start = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        slug = profile.slug_at(start)
        market = {
            "id": "market",
            "slug": slug,
            "question": "Ethereum Up or Down - September 19",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["up-token", "down-token"],
            "endDate": (start + timedelta(seconds=300)).isoformat(),
        }

        def parse(**overrides: object) -> object:
            return parse_events(
                [{"id": "event", "slug": slug, "markets": [market | overrides]}],
                profile=profile,
            )

        self.assertEqual(len(parse()), 1)
        for overrides in (
            {"outcomes": None},
            {"outcomes": ["Yes", "No"]},
            {"outcomes": ["Up", "Up"]},
            {"outcomes": [1, 2]},
            {"outcomes": "invalid"},
            {"clobTokenIds": ["same", "same"]},
            {"question": "Bitcoin Up or Down"},
            {"slug": slug.replace("eth", "btc")},
            {"endDate": (start + timedelta(seconds=900)).isoformat()},
            {"endDate": None},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(parse(**overrides), [])


if __name__ == "__main__":
    unittest.main()
