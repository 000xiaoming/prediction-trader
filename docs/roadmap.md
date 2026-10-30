# Roadmap

## Available workflow

- A portable paper-only Docker Compose stack, plus an explicit WSL relay mode.
- Independent analysis views and replay reports for BTC 5-minute, BTC 15-minute and
  ETH 5-minute Polymarket profiles.
- Persistent, isolated paper accounts for spot, perpetual and binary-event instruments,
  idempotent retries, explicit fees, manual funding events and binary-event settlements.
- Offline read-only history with event filters, stable pagination, dashboard CSV downloads
  and complete CSV / JSONL exports. Exports support analysis, not restoring account state.
- Read-only Docker diagnostics and verified online SQLite backups, with non-overwriting recovery
  to a new ledger file and stdin imports from the host.
- CI covering lint, types, unit and PostgreSQL tests, browser interactions, packaging and Docker.

Next operational work: automate PostgreSQL backup/restore drills, measure storage growth, and
add market-freshness alerts. SQLite recovery is tested; PostgreSQL still has a documented manual
workflow. See [operations](operations.md) and [paper operations](paper-trading.md).
The milestones below describe remaining research and execution work as well as completed foundations.

## M0: Safe scaffold — available

- Four-container local stack
- Typed decision path
- Paper adapter only
- Metrics, dashboard and tests

## M1: Read-only collection — available, diagnostics still evolving

- Official Polymarket V2 market and user-independent feeds
- Binance native WebSocket feed
- Append-only raw event journal with event and receive timestamps
- Reconnect, gap detection and sequence validation

## M2: Replay and paper execution — partial

Replay reports and explicit public-quote paper fills are available. Queue position, partial fills,
market impact and calibrated out-of-sample evaluation still require additional work and evidence.

- Deterministic event replay
- Queue-aware fill simulation
- Fees, slippage, partial fills and cancel latency
- Strategy calibration reports and out-of-sample evaluation

## M3: Shadow operation — planned

- Continuous live signals without order submission
- Executable-price and hypothetical-fill tracking
- Operational alerts and restart drills

## M4: Isolated-wallet canary — future gated work

Live submission is not implemented or enabled. Any future adapter needs a separate configuration
and the reconciliation, loss-limit and emergency-stop gates in [security](security.md).

- Official Polymarket V2 execution adapter
- Persist-before-submit outbox
- Startup and periodic reconciliation
- Supervised, short-duration, very small notional tests

## M5: Controlled expansion — planned

- Additional markets and strategy implementations
- Signing-service isolation if justified
- High-availability design only after measured need

