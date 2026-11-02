from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_paper_preview import inputs

from prediction_trader.trading.cli import main
from prediction_trader.trading.engine import PaperAccount
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import TradingError

D = Decimal


@pytest.mark.parametrize("side,limit,allowed", [
    ("buy", "101", True), ("buy", "102", True), ("buy", "100.99", False),
    ("sell", "100", True), ("sell", "99", True), ("sell", "100.01", False),
])
def test_price_limits_use_executable_side_and_reject_without_mutation(side, limit, allowed):
    spec, order, quote = inputs("perpetual")
    account = PaperAccount("alpha", D("1000"))
    order = replace(order, side=side, limit_price=D(limit))
    before = account.to_state()
    if allowed:
        fill = account.execute(spec, order, quote, now=quote.observed_at)
        assert fill.price == (quote.ask if side == "buy" else quote.bid)
    else:
        with pytest.raises(TradingError, match="price limit"):
            account.execute(spec, order, quote, now=quote.observed_at)
        assert account.to_state() == before


@pytest.mark.parametrize("limit", ["0", "-1", "NaN", "Infinity"])
def test_invalid_price_limits(limit):
    _, order, _ = inputs()
    with pytest.raises(TradingError, match="limit_price"):
        replace(order, limit_price=D(limit))


def test_persisted_limit_is_part_of_idempotency_and_old_states_remain_readable(tmp_path: Path):
    spec, order, quote = inputs()
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    guarded = replace(order, limit_price=D("101"))
    fill, account = ledger.execute(spec, guarded, quote, now=quote.observed_at)
    loaded = ledger.account("alpha")
    assert loaded.orders[order.client_order_id][0] == guarded
    assert ledger.execute(spec, guarded, quote, now=quote.observed_at)[0] == fill
    before = loaded.to_state(), ledger.history("alpha")
    with pytest.raises(TradingError, match="Idempotency conflict"):
        ledger.execute(spec, replace(guarded, limit_price=D("102")), quote, now=quote.observed_at)
    assert (ledger.account("alpha").to_state(), ledger.history("alpha")) == before
    legacy = account.to_state()
    del legacy["orders"][order.client_order_id]["order"]["limit_price"]
    assert PaperAccount.from_state(legacy).orders[order.client_order_id][0].limit_price is None


@pytest.mark.parametrize("command", ["preview", "paper-run"])
def test_cli_rejects_moved_price_without_recording(tmp_path, monkeypatch, capsys, command):
    spec, _, quote = inputs()
    market = MagicMock()
    market.__enter__.return_value = market
    market.snapshot.return_value = spec, replace(quote, observed_at=datetime.now(UTC))
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData", lambda: market)
    path = tmp_path / "paper.db"
    with pytest.raises(SystemExit) as exc:
        main([command, spec.instrument_id, "--portfolio", "alpha", "--side", "buy",
              "--quantity", "1", "--fee-bps", "10", "--client-order-id", "guarded",
              "--limit-price", "100", "--db", str(path)])
    assert exc.value.code == 2
    assert "price limit" in capsys.readouterr().err
    assert SQLitePaperLedger(path, read_only=True).account("alpha") is None
