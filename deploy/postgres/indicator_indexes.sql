-- Optional online migration for existing databases. Run with psql autocommit;
-- CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_indicator_market_window
    ON markets (slug, end_date);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_indicator_reference_window
    ON reference_prices (source, symbol, source_timestamp, observed_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_indicator_book_latest
    ON order_book_metrics (token_id, observed_at DESC, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_indicator_tick_window
    ON price_ticks (instrument, observed_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_indicator_trade_window
    ON market_trade_ticks (token_id, observed_at);
