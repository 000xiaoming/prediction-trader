"""Explicit local paper round trips on public quotes, in a separate verification ledger."""
import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from prediction_trader.trading.adapters import PublicMarketData
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import PaperOrder

CASES = {
    "binance_btc_spot": "0.0001",
    "binance_eth_spot": "0.003",
    "binance_btc_perpetual": "0.001",
    "binance_eth_perpetual": "0.01",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    ledger = SQLitePaperLedger("/data/deployment-check.sqlite3")
    manifest = Path("/data/deployment-check.json")
    cases = json.loads(manifest.read_text()) if manifest.exists() else dict(CASES)
    with httpx.Client(timeout=8) as client, PublicMarketData(client=client) as data:
        event_quote = None
        if not args.verify_only and not any(k.startswith("polymarket_event_") for k in cases):
            response = client.get("https://gamma-api.polymarket.com/markets", params={
                "active": "true", "closed": "false", "limit": 100,
                "order": "volume24hr", "ascending": "false",
            })
            response.raise_for_status()
            for market in response.json():
                if market.get("negRisk") or market.get("acceptingOrders") is not True:
                    continue
                instrument = f"polymarket_event_{market['id']}_yes"
                try:
                    spec, quote = data.snapshot(instrument, "verification20260919:" + instrument)
                    if (datetime.now(UTC) - quote.observed_at).total_seconds() > 5:
                        continue
                except ValueError:
                    continue
                # Execute the event first while its quote is fresh.
                cases = {instrument: str(spec.minimum_quantity), **cases}
                event_quote = spec, quote
                manifest.write_text(json.dumps(cases), encoding="utf-8")
                break
            else:
                raise RuntimeError("No fresh ordinary event in the bounded sample")
        for instrument, quantity in cases.items():
            portfolio = "verification20260919:" + instrument
            if not args.verify_only:
                if event_quote and event_quote[0].instrument_id == instrument:
                    spec, quote = event_quote
                else:
                    spec, quote = data.snapshot(instrument, portfolio)
                leverage = Decimal("2") if spec.kind == "perpetual" else Decimal("1")
                for sequence, side in enumerate(("buy", "sell"), 1):
                    order = PaperOrder(
                        "roundtrip-" + str(sequence), instrument, portfolio, side,
                        Decimal(quantity), Decimal("10"), leverage,
                        reduce_only=side == "sell" and spec.kind == "perpetual",
                    )
                    fill, account = ledger.execute(spec, order, quote)
                    duplicate, _ = ledger.execute(spec, order, quote)
                    assert fill == duplicate
                if spec.kind == "perpetual":
                    for sequence, side in enumerate(("sell", "buy"), 3):
                        order = PaperOrder(
                            "roundtrip-" + str(sequence), instrument, portfolio, side,
                            Decimal(quantity), Decimal("10"), leverage, reduce_only=side == "buy",
                        )
                        ledger.execute(spec, order, quote)
            account = ledger.account(portfolio)
            assert account is not None
            assert all(p.quantity == 0 for p in account.positions.values())
            summary = account.summary()
            assert summary["fill_count"] == (4 if instrument.endswith("perpetual") else 2)
            print(json.dumps({
                "instrument": instrument, "fills": summary["fill_count"],
                "cash": summary["cash"], "fees": summary["fees"], "flat": True,
            }), flush=True)


if __name__ == "__main__":
    main()
