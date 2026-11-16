import json
import sqlite3
from unittest.mock import Mock

import pytest
from test_multi_api import PublicFixture
from test_paper_retries import recorded

from prediction_trader.config import Settings
from prediction_trader.services import paper_trading
from prediction_trader.trading.cli import main
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import TradingError

PAYLOAD = {"instrument_id": "binance_btc_spot", "client_order_id": "lookup-test",
           "side": "buy", "quantity": "1", "fee_bps": "10", "limit_price": "101"}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    feed = PublicFixture()
    monkeypatch.setattr(paper_trading, "PublicMarketData", lambda: feed)
    service = paper_trading.PaperTradingGateway(Settings(
        _env_file=None, paper_ledger_path=str(tmp_path / "missing" / "paper.db"),
    ))
    return service, feed


def test_status_never_executes_missing_orders_or_creates_a_ledger(gateway, tmp_path):
    service, feed = gateway
    result = service.order_status(PAYLOAD)
    assert result["status"] == "not_recorded" and result["fill"] is None
    assert result["client_order_id"] == "lookup-test"
    assert result["portfolio_id"] == "paper:binance_btc_spot"
    assert service._ledger is None and not (tmp_path / "missing").exists() and feed.calls == 0


def test_status_confirms_recorded_payload_offline_without_mutation(gateway):
    service, feed = gateway
    executed = service.order(PAYLOAD)
    before = recorded(service.ledger)
    feed.offline = True
    result = service.order_status({**PAYLOAD, "quantity": "1.0", "limit_price": "101.00"})
    assert result["status"] == "recorded" and result["fill"] == executed["fill"]
    assert service.order_status({**PAYLOAD, "instrument_id": "binance_eth_spot"})[
        "status"
    ] == "not_recorded"
    assert recorded(service.ledger) == before and feed.calls == 1


@pytest.mark.parametrize("patch", [
    {"quantity": "2"}, {"side": "sell"}, {"fee_bps": "11"}, {"leverage": "2"},
    {"reduce_only": True}, {"limit_price": "102"},
])
def test_status_preserves_conflicting_orders(gateway, patch):
    service, feed = gateway
    service.order(PAYLOAD)
    before = recorded(service.ledger)
    with pytest.raises(TradingError, match="Idempotency conflict"):
        service.order_status({**PAYLOAD, **patch})
    assert recorded(service.ledger) == before and feed.calls == 1


def test_lookup_reads_committed_state_during_an_uncommitted_write(gateway):
    service, _ = gateway
    service.order(PAYLOAD)
    with sqlite3.connect(service.ledger.path) as writer:
        writer.execute("UPDATE paper_accounts SET state='uncommitted-invalid'")
        assert service.order_status(PAYLOAD)["status"] == "recorded"
        writer.rollback()


def test_cli_lookup_reports_missing_and_recorded_orders_offline(gateway, capsys, monkeypatch):
    service, _ = gateway
    service.order(PAYLOAD)
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData",
                        Mock(side_effect=AssertionError("Lookup must stay offline")))
    before = recorded(service.ledger)
    for name, status in (("lookup-test", "recorded"), ("missing", "not_recorded")):
        main(["order-status", "paper:binance_btc_spot", name, "--db", str(service.ledger.path)])
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == status
        if status == "recorded":
            assert result["order"]["limit_price"] == "101"
            assert result["fill"]["client_order_id"] == name
    assert recorded(service.ledger) == before


@pytest.mark.parametrize("portfolio,key", [("", "valid"), ("alpha", ""), ("alpha", "../invalid")])
def test_lookup_rejects_invalid_names_before_creating_paths(tmp_path, portfolio, key):
    ledger = SQLitePaperLedger(tmp_path / "missing" / "paper.db", read_only=True)
    with pytest.raises(TradingError):
        ledger.recorded_order(portfolio, key)
    assert not (tmp_path / "missing").exists()
