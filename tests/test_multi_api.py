import threading
from datetime import UTC, datetime
from decimal import Decimal
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from prediction_trader.config import Settings
from prediction_trader.services import indicator_server, paper_trading
from prediction_trader.trading.catalog import get_instrument
from prediction_trader.trading.models import Quote, TradingError


class PublicFixture:
    def __init__(self) -> None:
        self.calls = 0
        self.offline = False

    def snapshot(self, instrument: str, portfolio: str) -> tuple:
        self.calls += 1
        if self.offline:
            raise TradingError("offline")
        return get_instrument(instrument), Quote(
            instrument, portfolio, datetime.now(UTC), Decimal("100"), Decimal("101"),
            Decimal("100"), Decimal("100"),
        )

    def close(self) -> None:
        pass


def test_paper_gateway_persists_and_retries_without_network_or_duplicate_fill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    feed = PublicFixture()
    monkeypatch.setattr(paper_trading, "PublicMarketData", lambda: feed)
    settings = Settings(_env_file=None, paper_ledger_path=str(tmp_path / "paper.sqlite3"))
    gateway = paper_trading.PaperTradingGateway(settings)
    payload = {
        "instrument_id": "binance_btc_spot", "client_order_id": "retry-test",
        "side": "buy", "quantity": "1", "fee_bps": "10",
    }
    first = gateway.order(payload)
    assert first["account"]["cash"] == "898.899"
    feed.offline = True
    restarted = paper_trading.PaperTradingGateway(settings)
    second = restarted.order(payload)
    assert first == second
    assert feed.calls == 1
    with pytest.raises(TradingError, match="Idempotency conflict"):
        restarted.order({**payload, "quantity": "2"})
    feed.offline = False
    other = restarted.state("binance_eth_spot")
    assert other["account"] is None
    assert other["portfolio_id"] != first["account"]["portfolio_id"]
    assert restarted.ledger.account("paper:binance_btc_spot").summary()["fill_count"] == 1


@pytest.mark.parametrize("patch", [
    {"quantity": "NaN"}, {"fee_bps": "-1"}, {"fee_bps": 10},
    {"instrument_id": "https://localhost/private"}, {"reduce_only": "false"},
    {"leverage": 2}, {"unexpected": True},
])
def test_paper_gateway_rejects_bad_input_before_public_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patch: dict,
) -> None:
    feed = PublicFixture()
    monkeypatch.setattr(paper_trading, "PublicMarketData", lambda: feed)
    gateway = paper_trading.PaperTradingGateway(
        Settings(_env_file=None, paper_ledger_path=str(tmp_path / "ledger")),
    )
    with pytest.raises(TradingError):
        gateway.order({
            "instrument_id": "binance_btc_spot", "client_order_id": "invalid",
            "side": "buy", "quantity": "1", "fee_bps": "10", **patch,
        })
    assert feed.calls == 0


def test_profile_http_routes_are_isolated_and_paper_post_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    settings = Settings(
        _env_file=None, indicator_host="127.0.0.1", indicator_port=0,
        enabled_trading_profiles="polymarket_btc_15m,polymarket_eth_5m",
    )
    repository = Mock()
    reports = Mock()
    reports.latest.side_effect = lambda profile: {"profile_id": profile}
    gateway = Mock()
    gateway.order.return_value = {"mode": "paper", "fill": {}}
    readers = {}
    def reader_factory(repository: object, settings: Settings) -> Mock:
        reader = Mock()
        reader.read.return_value = {"profile": settings.profile.id}
        readers[settings.profile.id] = reader
        return reader
    monkeypatch.setattr(indicator_server, "MarketRepository", lambda _: repository)
    monkeypatch.setattr(indicator_server, "BacktestRepository", lambda _: reports)
    monkeypatch.setattr(indicator_server, "PaperTradingGateway", lambda _: gateway)
    monkeypatch.setattr(indicator_server, "IndicatorStateReader", reader_factory)
    servers = []
    ready, stop = threading.Event(), threading.Event()
    def server_factory(address: tuple, handler: type) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(address, handler)
        servers.append(server)
        ready.set()
        return server
    monkeypatch.setattr(indicator_server, "ThreadingHTTPServer", server_factory)
    thread = threading.Thread(
        target=indicator_server.IndicatorServerService(settings, stop).run, daemon=True,
    )
    thread.start()
    assert ready.wait(5)
    connection = HTTPConnection("127.0.0.1", servers[0].server_port, timeout=5)
    try:
        for profile in settings.enabled_profiles:
            for _ in range(2):
                connection.request("GET", "/api/state?profile=" + profile.id)
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read()) == {"profile": profile.id}
            assert readers[profile.id].read.call_count == 1
            connection.request("GET", "/api/reports?profile=" + profile.id)
            response = connection.getresponse()
            assert json.loads(response.read())["report"]["profile_id"] == profile.id
        connection.request("GET", "/api/state?profile=missing")
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        gateway.order.assert_not_called()
        for headers, status in [
            ({"Origin": "https://unrelated.example", "Content-Type": "application/json"}, 403),
            ({"Content-Type": "text/plain"}, 400),
            ({"Content-Type": "application/json"}, 200),
        ]:
            connection.request("POST", "/api/paper/orders", body="{}", headers=headers)
            response = connection.getresponse()
            assert response.status == status
            response.read()
        gateway.order.assert_called_once_with({})
        gateway.history.return_value = {
            "mode": "paper", "instrument_id": "binance_btc_spot",
            "portfolio_id": "paper:binance_btc_spot", "next_before": 42,
            "events": [{"sequence": 42, "event_type": "funding",
                        "observed_at": "2026-01-01T00:00:00+00:00",
                        "payload": {"result": "-0.0001"}}],
        }
        history_url = "/api/trading/history?instrument=binance_btc_spot&limit=1&before=99"
        connection.request("GET", history_url + "&event_type=all")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["next_before"] == 42
        gateway.history.assert_called_once_with(
            "binance_btc_spot", limit=1, before=99, event_type=None,
        )
        connection.request("GET", history_url + "&format=csv")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/csv; charset=utf-8"
        assert response.getheader("Content-Disposition") == (
            'attachment; filename="paper-history.csv"'
        )
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("X-Next-Before") == "42"
        body = response.read().decode("utf-8-sig")
        assert body.startswith("sequence,portfolio_id,event_type,")
        assert "-0.0001" in body
        for query in ("limit=x", "before=", "limit=1&limit=2", "format=html", "unknown=1"):
            gateway.history.reset_mock()
            connection.request("GET", "/api/trading/history?instrument=binance_btc_spot&" + query)
            response = connection.getresponse()
            assert response.status == 400
            response.read()
            gateway.history.assert_not_called()
        gateway.history.side_effect = TradingError("History limit must be in [1, 1000]")
        connection.request("GET", "/api/trading/history?instrument=binance_btc_spot&limit=0")
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        for method, path in (
            ("GET", "/api/trading/state?instrument=binance_btc_spot"),
            ("GET", "/api/trading/history?instrument=binance_btc_spot"),
            ("POST", "/api/paper/orders"),
        ):
            gateway.state.side_effect = ValueError("private ledger connection details")
            gateway.history.side_effect = ValueError("private ledger connection details")
            gateway.order.side_effect = ValueError("private ledger connection details")
            connection.request(
                method, path, body="{}" if method == "POST" else None,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 500
            assert "private ledger" not in response.read().decode()
        for malformed in ("{broken", "[NaN"):
            connection.request(
                "POST", "/api/paper/orders", body=malformed,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 400
            assert json.loads(response.read())["detail"] == (
                "Invalid or incomplete JSON request body"
            )
    finally:
        connection.close()
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
