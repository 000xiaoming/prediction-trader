import argparse
import json

from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository
from prediction_trader.replay import HistoricalReplay
from prediction_trader.replay_db import load_signal_replay_frames
from prediction_trader.strategies.cross_venue_lead import CrossVenueLeadStrategy


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay legacy BTC five-minute signals using recorded market data.",
        epilog="Only the polymarket_btc_5m trading profile supports this legacy replay.",
    )
    parser.parse_args(argv)
    settings = Settings()
    if not settings.profile.legacy_replay_supported:
        parser.error(
            "legacy replay supports only polymarket_btc_5m; "
            f"selected profile is {settings.profile.id}"
        )
    repository = MarketRepository(settings.database_url)
    try:
        frames = load_signal_replay_frames(repository)
    finally:
        repository.close()
    strategy = CrossVenueLeadStrategy(
        settings.cross_venue_min_btc_move,
        settings.cross_venue_probability_sensitivity,
    )
    result = HistoricalReplay(strategy).run(frames)
    print(
        json.dumps(
            {
                "mode": "signal_only",
                "profile_id": settings.profile.id,
                "frames": result.frames,
                "signals": len(result.signals),
                "strategy": strategy.name,
                "warning": "Midpoint replay does not estimate fills, PnL, or tradability.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
