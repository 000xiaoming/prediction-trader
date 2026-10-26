from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from prediction_trader.domain.models import Outcome
from prediction_trader.execution_replay import QuoteEvent
from prediction_trader.persistence import MarketRepository
from prediction_trader.profiles import DEFAULT_PROFILE, get_profile


def load_quote_events(repository: MarketRepository) -> list[QuoteEvent]:
    """Load only in-window quotes from the legacy BTC5 replay market family."""
    profile = get_profile(DEFAULT_PROFILE)
    statement = text(
        """
        SELECT tick.observed_at, tick.market_id, tick.outcome, tick.best_bid, tick.best_ask
        FROM price_ticks tick
        JOIN markets market ON market.id = tick.market_id
        WHERE tick.venue = 'polymarket'
          AND lower(market.question) LIKE 'bitcoin up or down%'
          AND market.slug = :slug_prefix || '-' ||
              (extract(epoch FROM market.end_date)::bigint - :duration_seconds)::text
          AND mod(extract(epoch FROM market.end_date), :duration_seconds) = 0
          AND tick.observed_at >= market.end_date - (:duration_seconds * interval '1 second')
          AND tick.observed_at < market.end_date
          AND tick.outcome IN ('YES', 'NO')
        ORDER BY tick.observed_at, tick.id
        """
    )
    with repository.engine.connect() as connection:
        rows = connection.execute(
            statement,
            {
                "slug_prefix": profile.slug_prefix,
                "duration_seconds": profile.duration_seconds,
            },
        ).all()
    return [
        QuoteEvent(
            timestamp=row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(row[0]),
            market_id=str(row[1]),
            outcome=Outcome(str(row[2])),
            bid=Decimal(row[3]) if row[3] is not None else None,
            ask=Decimal(row[4]) if row[4] is not None else None,
        )
        for row in rows
    ]
