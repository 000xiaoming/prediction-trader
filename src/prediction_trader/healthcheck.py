"""Check local runtime readiness, without using market connections or HTTP proxies."""

import os
from urllib.request import ProxyHandler, build_opener

from prometheus_client.parser import text_string_to_metric_families

_MAX_RESPONSE_BYTES = 1024 * 1024


def metrics_ready(body: str) -> bool:
    samples = [sample for family in text_string_to_metric_families(body)
               for sample in family.samples]
    for name, expected in (("prediction_trader_ready", 1), ("prediction_trader_mode", 0)):
        values = [sample.value for sample in samples if sample.name == name and not sample.labels]
        if values != [expected]:
            return False
    workers = [sample.value for sample in samples
               if sample.name == "prediction_trader_worker_alive"]
    return bool(workers) and all(value == 1 for value in workers)


def main() -> int:
    try:
        port = int(os.environ.get("METRICS_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid metrics port")
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/metrics", timeout=2) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES or not metrics_ready(body.decode("utf-8")):
            print("Trader is not ready: inspect runtime workers and startup logs.")
            return 1
    except (OSError, ValueError):
        print("Trader readiness unavailable: check the local metrics listener.")
        return 1
    print("Trader is ready in paper mode; required workers are alive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
