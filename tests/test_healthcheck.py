import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from prediction_trader import healthcheck

READY = (
    'prediction_trader_ready 1\nprediction_trader_mode 0\n'
    'prediction_trader_worker_alive{worker="indicators"} 1\n'
    'prediction_trader_worker_alive{worker="collector"} 1\n'
)


@pytest.mark.parametrize("body", [
    "", "unrelated_metric 1\n", READY.replace("ready 1", "ready 0"),
    READY.replace("mode 0", "mode 1"), READY.replace('collector"} 1', 'collector"} 0'),
    READY.replace("ready 1", "ready NaN"), READY + "prediction_trader_ready 1\n",
    'prediction_trader_ready 1\nprediction_trader_mode 0\n',
])
def test_unready_or_incomplete_metrics_are_not_healthy(body: str) -> None:
    assert not healthcheck.metrics_ready(body)


@pytest.mark.parametrize("body,code", [(READY, 0), (READY.replace("ready 1", "ready 0"), 1),
                                       ("not metrics at all", 1), ("x" * (1024 * 1024 + 1), 1)])
def test_http_200_alone_is_insufficient_and_local_checks_bypass_proxies(
    monkeypatch: pytest.MonkeyPatch, body: str, code: int,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            assert self.path == "/metrics"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("METRICS_PORT", str(server.server_port))
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    try:
        assert healthcheck.main() == code
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_listener_configuration_is_reported_without_tracebacks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], port: str,
) -> None:
    monkeypatch.setenv("METRICS_PORT", port)
    assert healthcheck.main() == 1
    assert "readiness unavailable" in capsys.readouterr().out
