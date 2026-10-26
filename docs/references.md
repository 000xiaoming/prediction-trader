# Version-sensitive references

Reviewed on 2026-08-22. Re-check these sources before enabling a live adapter because exchange
interfaces and geographic eligibility can change.

## Polymarket

- Official repositories: <https://github.com/orgs/Polymarket/repositories>
- CLOB V2 migration: <https://docs.polymarket.com/v2-migration>
- Market WebSocket channel: <https://docs.polymarket.com/api-reference/wss/market>
- User WebSocket channel: <https://docs.polymarket.com/api-reference/wss/user>
- Geographic eligibility API: <https://docs.polymarket.com/api-reference/geoblock>

The archived V1 clients are intentionally excluded. A future live adapter must use an official V2
SDK and must be contract-tested against the then-current documentation.

## Trading engine and market data

- NautilusTrader: <https://github.com/nautechsystems/nautilus_trader>
- CCXT: <https://github.com/ccxt/ccxt>

NautilusTrader is the intended long-term event engine, but it is not yet added as a dependency in
milestone M0. The typed domain boundaries are being kept small so that integration can be measured
instead of assumed. Latency-sensitive Binance collection should use the official native WebSocket;
CCXT is suitable for research and non-critical normalization.

## WSL and containers

- Microsoft WSL environment guide:
  <https://learn.microsoft.com/windows/wsl/setup/environment>
- Docker Desktop WSL2 backend:
  <https://docs.docker.com/desktop/features/wsl/>
- Docker WSL2 filesystem recommendations:
  <https://docs.docker.com/desktop/features/wsl/best-practices/>

The authoritative checkout is therefore kept under `~/projects`, not under `/mnt/c`.
