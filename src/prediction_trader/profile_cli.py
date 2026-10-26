"""List and check market profiles without opening databases or contacting venues."""

import argparse
import json
from typing import Any

from pydantic import ValidationError

from prediction_trader.config import Settings, TradingMode
from prediction_trader.profiles import get_profile, list_profiles
from prediction_trader.strategies.indicator_models import get_indicator_model


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inspect market profiles; no network or trading.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List all supported profiles")
    show = commands.add_parser("show", help="Show one profile or the configured active profile")
    show.add_argument("profile", nargs="?")
    check = commands.add_parser("check", help="Validate configuration and derived data bindings")
    check.add_argument("--profile", help="Check a profile without changing .env")
    args = parser.parse_args(argv)
    result: dict[str, Any]
    try:
        if args.command == "list":
            result = {"profiles": [profile.as_dict() for profile in list_profiles()]}
        elif args.command == "show" and args.profile:
            result = {"profile": get_profile(args.profile).as_dict()}
        else:
            settings = Settings(trading_profile=args.profile) if args.profile else Settings()
            if args.command == "check" and settings.trading_mode is TradingMode.LIVE:
                parser.error("Live execution is unavailable; set TRADING_MODE=paper")
            get_indicator_model(settings.profile.strategy_id)
            # Explicit allowlist: never print model_dump(), database URLs or credentials.
            result = {
                "status": "ok",
                "profile": settings.profile.as_dict(),
                "twap_window_seconds": settings.polymarket_chainlink_twap_window_seconds,
                "overridden_legacy_fields": list(settings.profile_overrides),
                "switching": "restart_required",
                "live_execution": False,
                "research_enabled": bool(settings.research_context_path),
            }
    except ValidationError:
        parser.error("Invalid configuration; check TRADING_PROFILE and typed environment settings")
    except ValueError as error:
        parser.error(str(error))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
