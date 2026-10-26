import json

from websockets.sync.client import connect

with connect("wss://ws-live-data.polymarket.com", open_timeout=20) as websocket:
    websocket.send(
        json.dumps(
            {
                "action": "subscribe",
                "subscriptions": [
                    {"topic": "crypto_prices_chainlink", "type": "update"}
                ],
            }
        )
    )
    for _ in range(10):
        print(repr(websocket.recv(timeout=10)))
