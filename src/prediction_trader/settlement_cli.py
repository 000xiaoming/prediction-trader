import json

from sqlalchemy import func, select

from prediction_trader.adapters.polymarket import GammaClient
from prediction_trader.config import Settings
from prediction_trader.persistence import MarketRepository, replay_fill_settlements
from prediction_trader.settlement import SettlementSynchronizer


def main() -> None:
    settings = Settings()
    repository = MarketRepository(settings.database_url)
    client = GammaClient(settings.polymarket_gamma_url)
    try:
        repository.create_schema()
        result = SettlementSynchronizer(repository, client).sync()
        with repository.engine.connect() as connection:
            stats = connection.execute(
                select(
                    func.count().label("settled_fills"),
                    func.count().filter(replay_fill_settlements.c.won).label("wins"),
                    func.coalesce(
                        func.sum(replay_fill_settlements.c.realized_pnl_usd), 0
                    ).label("realized_pnl_usd"),
                ).select_from(replay_fill_settlements)
            ).one()
    finally:
        client.close()
        repository.close()
    settled_fills = int(stats.settled_fills)
    wins = int(stats.wins)
    print(
        json.dumps(
            {
                "checked_markets": result.checked_markets,
                "resolved_markets": result.resolved_markets,
                "newly_settled_fills": result.newly_settled_fills,
                "total_settled_fills": settled_fills,
                "wins": wins,
                "losses": settled_fills - wins,
                "win_rate": str(wins / settled_fills) if settled_fills else None,
                "realized_pnl_usd": str(stats.realized_pnl_usd),
                "warning": "Realized PnL excludes market-specific fees.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
