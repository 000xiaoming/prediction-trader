"""Read-only deployment probe; it never submits exchange orders."""
import json
from datetime import UTC, datetime

import httpx

from prediction_trader.trading.adapters import PublicMarketData
from prediction_trader.trading.catalog import INSTRUMENTS
from prediction_trader.trading.models import jsonable


def main() -> None:
    with httpx.Client(timeout=15) as client, PublicMarketData(client=client) as data:
        for name in INSTRUMENTS:
            spec, quote = data.snapshot(name, "smoke:" + name)
            print(json.dumps({"instrument": jsonable(spec), "quote": jsonable(quote)}), flush=True)
        payload = client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": 100,
                    "order": "volume24hr", "ascending": "false"},
        )
        payload.raise_for_status()
        for item in payload.json():
            if item.get("negRisk") or item.get("acceptingOrders") is not True:
                continue
            try:
                spec, quote = data.event_snapshot(str(item["id"]), "YES", "smoke:event")
                if (datetime.now(UTC) - quote.observed_at).total_seconds() > 10:
                    continue
            except ValueError:
                continue
            print(json.dumps({"instrument": jsonable(spec), "quote": jsonable(quote)}), flush=True)
            return
        raise RuntimeError("No fresh ordinary YES/NO event in this bounded market sample")


if __name__ == "__main__":
    main()
