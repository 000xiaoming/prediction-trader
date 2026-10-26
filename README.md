# Prediction Trader

Run BTC 5m, BTC 15m and ETH 5m together, with independent dashboard views and replay reports.
The same dashboard also supports explicit paper orders for Binance BTC/ETH spot, USDT
perpetuals and ordinary Polymarket YES/NO events. See [玩法与操作](docs/market-profiles.md),
[模拟交易](docs/paper-trading.md) and [实施记录](docs/multi-market-plan.md).

For the September 2026 BTC five-minute improvements and the optional TradingAgents
research bridge, see [优化与集成说明](docs/tradingagents-integration.md).
The app collects public data, publishes reports and accepts explicitly requested simulated
orders through a durable paper ledger. Automatic strategy execution remains a research component.

Paper-first, event-driven foundation for researching crypto Up/Down market strategies. The
repository is intentionally not a turnkey profit bot. It starts with deterministic data flow,
auditable decisions, one risk gate, one order manager, and safe recovery.

## Safety status

- The current milestone is **paper trading only**.
- No private key is required or read.
- No live order adapter is enabled.
- Strategy modules can only emit signals; they cannot place orders.
- The risk engine may reduce or reject a target, but never create a trade.
- The original research order manager and the interactive paper ledger are separate execution paths.

See [Security](docs/security.md) and [Architecture](docs/architecture.md) before changing these
defaults.

The external components and version-sensitive assumptions reviewed for this scaffold are recorded
in [References](docs/references.md).

## Runtime layout

Four containers are started by Docker Compose:

1. `trader`: shared multi-profile collectors, indicators, settlement sync, scheduled replay,
   public venue adapters and explicit paper execution. SQLite paper accounts persist in `/data`.
2. `postgres`: transactional store for signals, decisions, risk results, order intents, fills,
   positions and audit records.
3. `prometheus`: scrapes health, latency, order, exposure and reconciliation metrics.
4. `grafana`: local dashboards and alert rules backed by Prometheus.

The intended strategy groups are code modules inside `trader`, not separate services;
only the early cross-venue research strategy is currently implemented:

- Cross-venue reference price
- Prediction-market microstructure
- Wallet-flow research (shadow-only initially)
- Contract structure and expiry

Market making is an execution policy, not a fifth directional strategy.

## Local environment

The intended development environment is Ubuntu 24.04 under WSL2 with Docker Desktop's shared
Linux engine. Keep the repository inside the WSL filesystem, for example:

```bash
cd ~/projects/prediction-trader
```

Do not put secrets in this repository. The `secrets/` path is ignored only as defense in depth;
production secret files must live outside the checkout.

## Quick start

On this configured Windows/WSL workstation, use the network relay and Compose overlays:

```bash
cd /path/to/prediction-trader
./pt start
./pt status
```

Open <http://localhost:8080> for analysis, profile reports and simulated trading.
This keeps the existing `.env` and data volumes. For a fresh installation:

Create a local configuration:

```bash
cp .env.example .env
```

Keep `TRADING_MODE=paper`, then validate and start:

```bash
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml up --build -d
docker compose -f deploy/compose.yaml ps
```

Open Grafana at <http://127.0.0.1:3000>. The initial local credentials are configured through
`.env`; change them before the dashboard is used beyond a private workstation.

## Polymarket read-only ingestion

The trader discovers active markets through Polymarket's public Gamma API. It filters the event,
market and question text, extracts the YES/NO CLOB token IDs, and upserts normalized rows into the
`events` and `markets` PostgreSQL tables. This path is public-data-only and never initializes a
wallet or trading client.

Relevant settings:

```dotenv
POLYMARKET_DISCOVERY_ENABLED=true
TRADING_PROFILE=polymarket_btc_5m
POLYMARKET_DISCOVERY_INTERVAL_SECONDS=60
POLYMARKET_DISCOVERY_PAGE_SIZE=100
```

Operational metrics include `polymarket_discovery_runs_total`,
`polymarket_discovered_markets`, and `polymarket_discovery_last_success_unixtime`.

The market-stream service loads the stored outcome tokens, subscribes to Polymarket's public CLOB
market WebSocket, handles the required application heartbeat, and refreshes its subscription as
market discovery changes. Latest bid, ask, midpoint and trade values are stored in
`market_quotes`. Grafana charts the `polymarket_outcome_price` series and WebSocket health metrics.

Append-only research history is stored in `price_ticks`. Binance's public market-data-only
`BTCUSDT@bookTicker` stream supplies the cross-venue reference bid and ask without an API key.
Binance updates are observed in real time and persisted at a configurable one-second interval to
bound database growth. The candle aggregator upserts one-minute OHLC rows into
`price_candles_1m`; Grafana reads those candles through its provisioned PostgreSQL datasource.

## Signal-only historical replay

Run the first cross-venue research strategy against time-aligned one-minute Binance and
Polymarket candles:

```bash
docker compose -f deploy/compose.yaml exec trader prediction-replay
```

The strategy only accepts `Bitcoin Up or Down` markets and refuses semantically different Bitcoin
contracts. It treats Binance as an explanatory reference, not the resolution truth: these markets
resolve from the Chainlink BTC/USD TWAP specified in their rules. This replay reports candidate
signals only. Midpoint candles cannot establish fills, executable PnL, queue position or slippage.
Those claims require the later event-level execution replay.

## Event-level paper execution replay

Run the same strategy through the quote-event execution simulator:

```bash
docker compose -f deploy/compose.yaml exec trader prediction-execution-replay
```

The simulator never uses a quote observed before the signal. It waits for the first executable ask
after the configured order latency, applies configurable slippage, and leaves the order unfilled if
no quote arrives before the fill timeout. Open positions are marked against the latest recorded bid.
The reported PnL is therefore unrealized mark-to-market research output; it does not yet include
market resolution, queue position, partial fills, or market-specific fees. All execution remains
paper-only and requires no wallet or private key.

Each execution replay is persisted atomically in PostgreSQL. The ledger records the run parameters,
signals, simulated fills, marked equity points and marked maximum drawdown. Grafana displays the
latest replay PnL, maximum drawdown, fill count and equity curve. A zero-signal replay is still
recorded so research runs are auditable rather than silently discarded.

Synchronize final outcomes for replayed markets from Polymarket's public Gamma API:

```bash
docker compose -f deploy/compose.yaml exec trader prediction-settlement-sync
```

Only a closed binary market with an unambiguous `1/0` outcome-price pair is treated as resolved.
The settlement ledger records the winner, payout, realized PnL and win/loss status without changing
the original simulated fill. Grafana displays realized PnL, win rate and settled-fill coverage for
the latest replay. The trader also retries unresolved replay markets every 60 seconds by default;
the CLI is available for an immediate manual sync. Realized PnL still excludes market-specific fees.

## BTC five-minute indicator dashboard

The dashboard now follows the selected profile, including ETH 5m and BTC 15m.
The details below describe the original BTC 5m use case; see the profile guide for switching
and the capability limits of the legacy replay commands.

The primary research surface is now a read-only decision dashboard rather than the early
cross-venue lead strategy. It consumes Polymarket's public Chainlink BTC/USD RTDS stream and the
public CLOB market WebSocket. TWAP uses the dedicated 30/60-second topic and a separate stored
source from legacy spot data. Missing opening observations or stale data suppress the model.
Open <http://localhost:8080> after publishing the Windows localhost
relay. The page shows the current and next five-minute windows, current and opening Chainlink TWAP,
remaining time, short-horizon volatility and momentum, an explicitly experimental remaining-time
probability, executable bids and asks, spread, near-touch and total depth, imbalance, quote-change
speed and recent trade direction.

The embedded calculator accepts a manual stake, actual buy price, planned sell price and target
profit. It includes taker fees on both entry and pre-resolution exit and reports shares, fees,
exit PnL, settlement PnL, break-even win probability, break-even sell price and the minimum sell
price for the requested profit. It never submits an order. The former cross-venue strategy remains
available only as historical research code and is not a production focus.

Stop the stack:

```bash
docker compose -f deploy/compose.yaml down
```

## Developer workflow

Python 3.12 is required. Until `uv` is installed, a standard virtual environment works:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy
```

If package or image downloads fail while WSL is configured to use a local Windows proxy, verify
that the configured proxy port is actually listening and is reachable from WSL. Do not delete or
replace an existing workstation proxy configuration merely to make a build pass.

## Decision ownership

The trading path is deliberately split:

```text
Market data
  -> StrategySignal[]
  -> fused fair probability
  -> TargetPosition
  -> RiskDecision
  -> ExecutionPlan
  -> OrderIntent
  -> OrderManager
  -> ExecutionAdapter
```

The decision engine decides **whether and how much exposure is desired**. The risk engine decides
**whether that exposure is allowed**. The execution planner decides **maker/taker, price, size and
time-in-force**. The order manager executes the approved plan idempotently and reconciles remote
state; it does not invent economic intent.

## Current milestone

This initial scaffold provides:

- typed domain events and decision objects;
- strategy protocol and four strategy-group identifiers;
- probability fusion, fixed-risk targeting and fail-closed risk checks;
- execution planning and a paper adapter boundary;
- four-container Compose layout;
- Prometheus/Grafana provisioning;
- architecture, security and operating documents;
- unit tests for the decision path.
- live read-only Polymarket Gamma market discovery and PostgreSQL persistence;
- Prometheus visibility for discovery success, failure, count and freshness.

Next milestones are listed in [Roadmap](docs/roadmap.md).
