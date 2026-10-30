# Prediction Trader

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/000xiaoming/prediction-trader/actions/workflows/ci.yml/badge.svg)](https://github.com/000xiaoming/prediction-trader/actions/workflows/ci.yml)

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

## Requirements

- Docker Engine or Docker Desktop with the Docker Compose plugin.
- Bash (Linux, macOS, or WSL). Python is included in the container image.
- Python 3.12 for development outside Docker.
- Access to public Polymarket and Binance market-data endpoints; no exchange API key is needed.

On Windows, use a WSL Linux checkout and enable Docker Desktop integration for that distribution.
The optional `wsl-relay` mode requires Compose 2.24.4 or newer and additional Windows setup.

Do not put secrets in this repository. The `secrets/` path is ignored only as defense in depth;
production secret files must live outside the checkout.

## Quick start

For a fresh checkout:

```bash
git clone https://github.com/000xiaoming/prediction-trader.git
cd prediction-trader
./pt init
# Review .env, including the local database and Grafana credentials.
./pt check
./pt start
./pt status
```

- Analysis and paper trading: <http://127.0.0.1:8080>.
- Grafana: <http://127.0.0.1:3000>; credentials come from `.env`.
- Startup logs: `./pt logs -f trader`.
- Read-only deployment diagnostics: `./pt doctor` (exit 1 if services need attention).
- Stop without deleting data: `./pt stop`.

`./pt init` preserves an existing `.env`. New checkouts use the portable Compose stack,
bind published ports to loopback, and do not start a Windows proxy or mount another project.
Change `INDICATOR_PUBLISHED_PORT` or `GRAFANA_PUBLISHED_PORT` if a host port is occupied.

Missing market history can initially produce a waiting/stale-data state. Leave the collectors
running until a complete market window is observed; do not interpret missing data as a signal.

For WSL networking, optional research mounts, and migration from the original workstation setup,
see [Installation and troubleshooting (中文)](docs/installation.md).
`./pt --help` lists all launcher commands. Equivalent portable Compose commands are:

```bash
docker compose --env-file .env -f deploy/compose.yaml config --quiet
docker compose --env-file .env -f deploy/compose.yaml up --build -d
```

The paper-order HTTP API has no user authentication. Keep it on loopback; public repository
visibility does not make the running dashboard suitable for internet exposure.

## Paper history and exports

The paper-trading dashboard includes per-instrument event history with filters for fills,
funding, settlements and marks. Browse older pages or download the displayed page as CSV.
History reads use the local ledger without fetching quotes, placing orders or recording marks.

With the Docker stack running:

```bash
./pt paper history paper:binance_btc_spot --event-type fill --limit 50
mkdir -p exports
./pt paper export paper:binance_btc_spot --format csv > exports/btc-spot.csv
./pt paper export paper:binance_btc_spot --format jsonl > exports/btc-spot.jsonl
```

`history` returns newest-first events and an exclusive `next_before` cursor for the next page.
`export` streams the **complete** matching history oldest first from one SQLite snapshot;
it has no 100/1000-event truncation. Both commands also work outside Docker with
`prediction-paper ... --db /path/to/paper.db` and do not need market connectivity.
Decimal strings and the original event payload are preserved. CSV protects spreadsheet cells
from formula interpretation; JSONL preserves the original values. `exports/` is ignored by Git
and excluded from Docker builds. These exports are analysis records, not restorable backups.

See [paper-history usage and HTTP API (中文)](docs/paper-trading.md#查询和导出流水).

## Deployment diagnostics and ledger recovery

`./pt doctor` checks Compose configuration, Docker availability and the four service containers.
It includes stopped containers, requires healthy trader/PostgreSQL containers, and reports
actionable failures without starting services or printing expanded configuration values.
It does not test market-data freshness. `./pt build trader` updates the image without starting it.

With the current image available, maintenance also works while the main application is stopped:

```bash
./pt ledger backup /data/backups/paper-snapshot.sqlite3
./pt ledger verify-backup /data/backups/paper-snapshot.sqlite3
./pt ledger restore /data/backups/paper-snapshot.sqlite3 --output /data/paper-restored.sqlite3
```

The online SQLite snapshot includes committed WAL data, all portfolios, positions and idempotency
records. Backups are verified before publication; existing targets and SQLite sidecars are never
overwritten. Recovery writes a new ledger and leaves the active ledger selection unchanged.
The maintenance container does not start dependencies, collectors or the Windows relay.
Copy completed backups off the data volume; PostgreSQL, monitoring volumes and `.env` are separate.
`backups/` is excluded from Git and Docker builds. See [operations and recovery (中文)](docs/operations.md)
for stdin import, container-path selection, validation and PostgreSQL restore drills.

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
Open <http://127.0.0.1:8080> with the default portable deployment.
The page shows the current and next five-minute windows, current and opening Chainlink TWAP,
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

Use Python 3.12 in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
make check
make build
```

`make check` runs Ruff, Mypy and pytest. PostgreSQL and browser tests are opt-in locally;
CI supplies a disposable PostgreSQL 17 service and Chrome to run them as well.
See [Contributing](CONTRIBUTING.md) for those commands and the CI environment.

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

Implemented capabilities include:

- typed domain events and decision objects;
- strategy protocol and four strategy-group identifiers;
- probability fusion, fixed-risk targeting and fail-closed risk checks;
- execution planning and a paper adapter boundary;
- four-container Compose layout;
- Prometheus/Grafana provisioning;
- architecture, security and operating documents;
- multi-profile collectors, dashboard views and independent replay reports;
- explicit simulated spot, perpetual and binary-event orders with a persistent paper ledger;
- unit, PostgreSQL integration, browser interaction and deployment configuration tests;
- GitHub Actions checks for source quality, packaging and a fresh Compose configuration;
- live read-only Polymarket Gamma market discovery and PostgreSQL persistence;
- Prometheus visibility for discovery success, failure, count and freshness.

Next milestones are listed in [Roadmap](docs/roadmap.md).

## License

The repository does not currently specify an open-source license.
