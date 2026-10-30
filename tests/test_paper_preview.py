import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prediction_trader.trading.catalog import get_instrument
from prediction_trader.trading.cli import main
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import PaperOrder, Quote, TradingError
from prediction_trader.trading.preview import preview_order

NOW = datetime(2026, 1, 1, tzinfo=UTC)
D = Decimal


def inputs(kind: str = "spot") -> tuple:
    spec = get_instrument("binance_btc_" + kind)
    order = PaperOrder("preview-1", spec.instrument_id, "alpha", "buy", D("1"), D("10"),
                       D("2") if kind == "perpetual" else D("1"))
    quote = Quote(spec.instrument_id, "alpha", NOW, D("100"), D("101"), D("100"), D("100"),
                  mark_price=D("100") if kind == "perpetual" else None)
    return spec, order, quote


@pytest.mark.parametrize("kind", ["spot", "perpetual"])
def test_preview_matches_later_explicit_execution_without_creating_a_ledger(
    tmp_path: Path, kind: str,
) -> None:
    spec, order, quote = inputs(kind)
    path = tmp_path / "missing" / "paper.db"
    first = preview_order(path, spec, order, quote, now=NOW)
    assert preview_order(path, spec, order, quote, now=NOW) == first
    assert first["preview"] and not first["persisted"] and not first["account_exists"]
    assert not path.parent.exists()
    assert first["account_before"]["fill_count"] == 0
    assert first["account_after"]["fill_count"] == 1
    ledger = SQLitePaperLedger(path)
    _, account = ledger.execute(spec, order, quote, now=NOW)
    assert account.summary() == first["account_after"]
    assert ledger.status()["event_count"] == 1  # Preview never consumed the order ID.


def test_preview_uses_existing_cash_and_positions_without_changing_audit_history(
    tmp_path: Path,
) -> None:
    spec, order, quote = inputs()
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.execute(spec, replace(order, quantity=D("2")), quote, now=NOW)
    before = ledger.account("alpha").to_state(), ledger.history("alpha")
    sell = replace(order, client_order_id="sell-preview", side="sell")
    later = replace(quote, bid=D("110"), ask=D("111"), observed_at=NOW + timedelta(seconds=1))
    result = preview_order(ledger.path, spec, sell, later, initial_cash=D("999999"),
                           now=later.observed_at)
    assert result["account_exists"] and D(result["account_before"]["cash"]) == D("797.798")
    assert D(result["account_after"]["cash"]) == D("907.688")
    assert D(result["fill"]["realized_pnl"]) == D("9")
    assert result["account_after"]["positions"][0]["quantity"] == "1"
    assert (ledger.account("alpha").to_state(), ledger.history("alpha")) == before
    with pytest.raises(TradingError, match="already recorded"):
        preview_order(ledger.path, spec, replace(order, quantity=D("2")), later,
                      now=later.observed_at)


@pytest.mark.parametrize("invalid", ["stale", "cash", "quantity"])
def test_preview_enforces_execution_limits_without_persisting_failures(
    tmp_path: Path, invalid: str,
) -> None:
    spec, order, quote = inputs()
    path = tmp_path / "missing" / "paper.db"
    if invalid == "quantity":
        order = replace(order, quantity=D("0.000000000001"))
    with pytest.raises(TradingError):
        preview_order(path, spec, order, quote,
                      initial_cash=D("1") if invalid == "cash" else D("1000"),
                      now=NOW + timedelta(seconds=11) if invalid == "stale" else NOW)
    assert not path.parent.exists()


@pytest.mark.parametrize("sizing", [["--quantity", "1"], ["--stake", "101"]])
def test_cli_preview_fetches_public_quotes_but_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    sizing: list[str],
) -> None:
    spec, _, quote = inputs()
    market = MagicMock()
    market.__enter__.return_value = market
    market.snapshot.return_value = spec, replace(quote, observed_at=datetime.now(UTC))
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData", lambda: market)
    path = tmp_path / "missing" / "paper.db"
    main(["preview", spec.instrument_id, "--portfolio", "alpha", "--side", "buy", *sizing,
          "--fee-bps", "10", "--client-order-id", "preview-1", "--db", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert result["preview"] and not result["persisted"]
    assert D(result["fill"]["quantity"]) == 1 and D(result["fill"]["fee"]) == D("0.101")
    assert not path.parent.exists()
    market.snapshot.assert_called_once_with(spec.instrument_id, "alpha")
