import json
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from prediction_trader.config import Settings
from prediction_trader.profiles import list_profiles
from prediction_trader.services import indicator_server


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("profile_id", [None, "polymarket_btc_15m", "polymarket_eth_5m"])
def test_api_uses_cache_and_returns_no_store_without_exposing_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fails: bool,
    profile_id: str | None,
    error_type: type[Exception],
) -> None:
    settings = Settings(
        _env_file=None,
        indicator_host="127.0.0.1",
        indicator_port=0,
        indicator_cache_ttl_seconds=5,
        **({"trading_profile": profile_id} if profile_id else {}),
    )
    stop = threading.Event()
    ready = threading.Event()
    repository = Mock()
    reader = Mock()
    if fails:
        reader.read.side_effect = error_type("private database connection details")
    else:
        reader.read.return_value = {"status": "ok"}
    repository_factory = Mock(return_value=repository)
    reader_factory = Mock(return_value=reader)
    cache_factory = Mock(wraps=indicator_server.SnapshotCache)
    monkeypatch.setattr(indicator_server, "MarketRepository", repository_factory)
    monkeypatch.setattr(indicator_server, "IndicatorStateReader", reader_factory)
    monkeypatch.setattr(indicator_server, "SnapshotCache", cache_factory)
    servers: list[ThreadingHTTPServer] = []

    def make_server(
        address: tuple[str, int], handler: type[BaseHTTPRequestHandler]
    ) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(address, handler)
        servers.append(server)
        ready.set()
        return server

    monkeypatch.setattr(indicator_server, "ThreadingHTTPServer", make_server)
    service = indicator_server.IndicatorServerService(settings, stop)
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    try:
        assert ready.wait(timeout=5)
        connection = HTTPConnection("127.0.0.1", servers[0].server_port, timeout=5)
        try:
            connection.request("GET", "/api/profiles")
            response = connection.getresponse()
            metadata = json.loads(response.read())
            assert response.status == 200
            assert response.getheader("Cache-Control") == "no-store"
            assert metadata == {
                "active": settings.profile.as_dict(),
                "enabled": [settings.profile.as_dict()],
                "profiles": [profile.as_dict() for profile in list_profiles()],
                "switching": "view_only",
            }
            assert metadata["active"]["id"] == (profile_id or "polymarket_btc_5m")
            assert settings.database_url not in json.dumps(metadata)
            reader.read.assert_not_called()
            for _ in range(2):
                connection.request("GET", "/api/state")
                response = connection.getresponse()
                body = response.read().decode()
                assert response.status == (500 if fails else 200)
                assert response.getheader("Cache-Control") == "no-store"
                assert "private database connection details" not in body
                assert json.loads(body) == (
                    {"status": "error", "detail": "Indicator state unavailable"}
                    if fails
                    else {"status": "ok"}
                )
        finally:
            connection.close()
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    reader_factory.assert_called_once_with(repository, settings=settings)
    cache_factory.assert_called_once_with(
        reader.read,
        ttl_seconds=settings.indicator_cache_ttl_seconds,
        window_seconds=settings.profile.duration_seconds,
    )
    assert reader.read.call_count == (2 if fails else 1)
    repository.close.assert_called_once_with()
    if fails:
        assert "Unable to load indicator snapshot" in caplog.text
        assert "private database connection details" in caplog.text


def test_server_bind_failure_closes_constructed_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    repository, paper = Mock(), Mock()
    monkeypatch.setattr(indicator_server, "MarketRepository", lambda _: repository)
    monkeypatch.setattr(indicator_server, "PaperTradingGateway", lambda _: paper)
    monkeypatch.setattr(
        indicator_server, "ThreadingHTTPServer", Mock(side_effect=OSError("address in use"))
    )
    with pytest.raises(OSError, match="address in use"):
        indicator_server.IndicatorServerService(Settings(_env_file=None), threading.Event()).run()
    repository.close.assert_called_once_with()
    paper.close.assert_called_once_with()


def test_reader_startup_failure_closes_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = Mock()
    monkeypatch.setattr(indicator_server, "MarketRepository", lambda _: repository)
    monkeypatch.setattr(
        indicator_server, "IndicatorStateReader", Mock(side_effect=ValueError("invalid reader"))
    )
    with pytest.raises(ValueError, match="invalid reader"):
        indicator_server.IndicatorServerService(Settings(_env_file=None), threading.Event()).run()
    repository.close.assert_called_once_with()
