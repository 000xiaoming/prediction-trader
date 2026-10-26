import argparse
import json

from prediction_trader.config import Settings
from prediction_trader.execution_replay import EventExecutionReplay
from prediction_trader.execution_replay_db import load_quote_events
from prediction_trader.persistence import MarketRepository
from prediction_trader.replay import HistoricalReplay
from prediction_trader.replay_db import load_signal_replay_frames
from prediction_trader.replay_ledger import ReplayLedger
from prediction_trader.strategies.cross_venue_lead import CrossVenueLeadStrategy


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay legacy BTC five-minute executions using recorded market data.",
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
        repository.create_schema()
        frames = load_signal_replay_frames(repository)
        quotes = load_quote_events(repository)
        strategy = CrossVenueLeadStrategy(
            settings.cross_venue_min_btc_move,
            settings.cross_venue_probability_sensitivity,
        )
        signals = list(HistoricalReplay(strategy).iter_signals(frames))
        replay = EventExecutionReplay(
            latency_ms=settings.replay_order_latency_ms,
            max_fill_wait_ms=settings.replay_max_fill_wait_ms,
            slippage_bps=settings.replay_slippage_bps,
            notional_usd=settings.replay_notional_usd,
        ).run(signals, quotes)
        parameters = {
            "profile_id": settings.profile.id,
            "latency_ms": settings.replay_order_latency_ms,
            "max_fill_wait_ms": settings.replay_max_fill_wait_ms,
            "slippage_bps": str(settings.replay_slippage_bps),
            "notional_usd": str(settings.replay_notional_usd),
            "min_btc_move": str(settings.cross_venue_min_btc_move),
        }
        run_id, max_drawdown = ReplayLedger(repository).record(
            strategy_name=strategy.name,
            frames=len(frames),
            signals=signals,
            replay=replay,
            parameters=parameters,
        )
    finally:
        repository.close()
    print(
        json.dumps(
            {
                "mode": "event_execution_mark_to_market",
                "profile_id": settings.profile.id,
                "run_id": run_id,
                "frames": len(frames),
                "signals": replay.signals,
                "filled": len(replay.filled),
                "unfilled": replay.unfilled,
                "marked_pnl_usd": str(replay.marked_pnl_usd),
                "max_drawdown_usd": str(max_drawdown),
                "warning": (
                    "Marked PnL is unrealized and excludes market-specific fees and resolution."
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
