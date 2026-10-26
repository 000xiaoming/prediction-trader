from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from prediction_trader.persistence import MarketRepository
from prediction_trader.profiles import DEFAULT_PROFILE, get_profile
from prediction_trader.replay import ReplayFrame


def load_signal_replay_frames(repository: MarketRepository) -> list[ReplayFrame]:
    """Legacy BTC5-only frames; never blend other assets or window lengths."""
    profile = get_profile(DEFAULT_PROFILE)
    statement = text(
        """
        WITH binance AS (
            SELECT bucket, close,
                   lag(close) OVER (ORDER BY bucket) AS previous_close
            FROM price_candles_1m
            WHERE venue = 'binance' AND instrument = :reference_instrument
        )
        SELECT yes_candle.bucket, market.id, market.question,
               yes_candle.close AS yes_mid, no_candle.close AS no_mid,
               binance.close AS btc_close, binance.previous_close AS btc_previous,
               greatest(0, extract(epoch FROM (market.end_date - yes_candle.bucket)))::bigint
                   AS seconds_to_expiry
        FROM markets market
        JOIN price_candles_1m yes_candle
          ON yes_candle.venue = 'polymarket'
         AND yes_candle.instrument = market.yes_token_id
        JOIN price_candles_1m no_candle
          ON no_candle.venue = 'polymarket'
         AND no_candle.instrument = market.no_token_id
         AND no_candle.bucket = yes_candle.bucket
        JOIN binance ON binance.bucket = yes_candle.bucket
        WHERE lower(market.question) LIKE 'bitcoin up or down%'
          AND market.slug = :slug_prefix || '-' ||
              (extract(epoch FROM market.end_date)::bigint - :duration_seconds)::text
          AND mod(extract(epoch FROM market.end_date), :duration_seconds) = 0
          AND yes_candle.bucket >= market.end_date - (:duration_seconds * interval '1 second')
          AND yes_candle.bucket < market.end_date
          AND binance.previous_close IS NOT NULL
        ORDER BY yes_candle.bucket, market.id
        """
    )
    with repository.engine.connect() as connection:
        rows = connection.execute(
            statement,
            {
                "reference_instrument": profile.binance_symbol.upper(),
                "slug_prefix": profile.slug_prefix,
                "duration_seconds": profile.duration_seconds,
            },
        ).all()
    return [
        ReplayFrame(
            timestamp=row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(row[0]),
            market_id=str(row[1]),
            question=str(row[2]),
            yes_bid=Decimal(row[3]),
            yes_ask=Decimal(row[3]),
            no_bid=Decimal(row[4]),
            no_ask=Decimal(row[4]),
            binance_price=Decimal(row[5]),
            binance_price_lookback=Decimal(row[6]),
            seconds_to_expiry=int(row[7]),
        )
        for row in rows
    ]
