"""Command-line profile replay using locally recorded history only."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from prediction_trader.backtesting.engine import BacktestParameters
from prediction_trader.backtesting.repository import BacktestRepository
from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository
from prediction_trader.profiles import DEFAULT_PROFILE, get_profile, list_profiles


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use an ISO-8601 timestamp with timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone, for example +00:00")
    return result.astimezone(UTC)


def _decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Expected a finite decimal number") from exc
    if not result.is_finite():
        raise argparse.ArgumentTypeError("Expected a finite decimal number")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay recorded markets with isolated profile reports; no live orders.",
    )
    parser.add_argument(
        "--profile", choices=[profile.id for profile in list_profiles()], default=DEFAULT_PROFILE
    )
    parser.add_argument(
        "--start", type=_timestamp, help="Inclusive market start time; default end-24h"
    )
    parser.add_argument(
        "--end", type=_timestamp, help="Exclusive market start cutoff; default as-of"
    )
    parser.add_argument(
        "--as-of", type=_timestamp, help="Observation cutoff; default current UTC time"
    )
    parser.add_argument("--decision-delay-seconds", type=int, default=60)
    parser.add_argument("--latency-ms", type=int, default=250)
    parser.add_argument("--timeout-seconds", type=int, default=5)
    parser.add_argument("--quote-max-age-seconds", type=int, default=5)
    parser.add_argument("--reference-max-age-seconds", type=int, default=5)
    parser.add_argument("--slippage-bps", type=_decimal, default=Decimal("5"))
    parser.add_argument("--min-edge", type=_decimal, default=Decimal("0.03"))
    parser.add_argument("--stake-usd", type=_decimal, default=Decimal("10"))
    parser.add_argument("--twap-window-seconds", type=int, choices=(30, 60), default=60)
    parser.add_argument("--max-markets", type=int, default=5000)
    parser.add_argument(
        "--output", type=Path, help="Optional report JSON file (report is also in DB)"
    )
    args = parser.parse_args(argv)
    as_of = args.as_of or datetime.now(UTC)
    end = args.end or as_of
    start = args.start or end - timedelta(hours=24)
    profile = get_profile(args.profile)
    try:
        params = BacktestParameters(
            start=start,
            end=end,
            as_of=as_of,
            decision_delay_seconds=args.decision_delay_seconds,
            latency_ms=args.latency_ms,
            timeout_seconds=args.timeout_seconds,
            quote_max_age_seconds=args.quote_max_age_seconds,
            reference_max_age_seconds=args.reference_max_age_seconds,
            slippage_bps=args.slippage_bps,
            min_edge=args.min_edge,
            stake_usd=args.stake_usd,
            twap_window_seconds=args.twap_window_seconds,
            max_markets=args.max_markets,
        )
        params.validate_profile(profile)
    except ValueError as exc:
        parser.error(str(exc))
    settings = Settings()
    repository = MarketRepository(settings.database_url)
    try:
        report = BacktestRepository(repository).run(profile, params)
    finally:
        repository.close()
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
