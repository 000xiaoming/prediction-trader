# Roadmap

## M0: Safe scaffold

- Four-container local stack
- Typed decision path
- Paper adapter only
- Metrics, dashboard and tests

## M1: Read-only collection

- Official Polymarket V2 market and user-independent feeds
- Binance native WebSocket feed
- Append-only raw event journal with event and receive timestamps
- Reconnect, gap detection and sequence validation

## M2: Replay and paper execution

- Deterministic event replay
- Queue-aware fill simulation
- Fees, slippage, partial fills and cancel latency
- Strategy calibration reports and out-of-sample evaluation

## M3: Shadow operation

- Continuous live signals without order submission
- Executable-price and hypothetical-fill tracking
- Operational alerts and restart drills

## M4: Isolated-wallet canary

- Official Polymarket V2 execution adapter
- Persist-before-submit outbox
- Startup and periodic reconciliation
- Supervised, short-duration, very small notional tests

## M5: Controlled expansion

- Additional markets and strategy implementations
- Signing-service isolation if justified
- High-availability design only after measured need

