"""Read-only checks against the deployed application on its internal loopback."""
import json
import urllib.request


def get(path: str) -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=10) as response:
        return json.load(response)

profiles = get("/api/profiles")
assert len(profiles["enabled"]) == 3
for profile in profiles["enabled"]:
    key = profile["id"]
    state = get("/api/state?profile=" + key)
    report = get("/api/reports?profile=" + key)
    assert state["profile"]["id"] == key
    assert state["status"] == "ok"
    assert report["report"]["profile_id"] == key
    print(json.dumps({
        "profile": key, "status": state["status"], "quality": state["quality"],
        "research": state["research"]["status"],
        "market_count": report["report"]["metrics"]["market_count"],
        "prediction_count": report["report"]["metrics"]["prediction_count"],
        "fill_count": report["report"]["metrics"]["fill_count"],
        "settled": report["report"]["metrics"]["resolved_prediction_count"],
    }), flush=True)
with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5) as response:
    metrics = response.read().decode()
assert "prediction_trader_ready 1.0" in metrics
workers = [line for line in metrics.splitlines()
           if line.startswith("prediction_trader_worker_alive{")]
assert len(workers) == 12 and all(line.endswith(" 1.0") for line in workers)
print("12 required workers alive; readiness=1")

