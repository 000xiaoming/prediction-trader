import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from test_paper_preview import inputs

from prediction_trader.trading.activity import activity_report, summarize_events
from prediction_trader.trading.cli import main
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import TradingError

D = Decimal


def test_activity_uses_all_matching_events_and_keeps_accounts_isolated(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    spec, order, quote = inputs()
    ledger.execute(spec, order, quote, now=quote.observed_at)
    ledger.execute(spec, order, quote, now=quote.observed_at)  # Idempotent retry is not activity.
    ledger.execute(spec, replace(order, portfolio_id="beta"), replace(quote, portfolio_id="beta"),
                   now=quote.observed_at)
    later = replace(quote, bid=D("110"), ask=D("111"),
                    observed_at=quote.observed_at + timedelta(seconds=1))
    ledger.execute(spec, replace(order, client_order_id="sell", side="sell", quantity=D("0.5")),
                   later, now=later.observed_at)
    ledger.mark(later, now=later.observed_at)
    before = ledger.account("alpha").to_state(), ledger.history("alpha")
    report = activity_report(SQLitePaperLedger(ledger.path, read_only=True), "alpha")
    assert report["event_count"] == 3
    assert report["events_by_type"] == {"fill": 2, "mark": 1, "funding": 0, "settlement": 0}
    assert report["first_sequence"] == 1 and report["last_sequence"] == 4
    group, = report["instruments"]
    assert group["quote_currency"] == "USDT"
    for key, value in {"buy_quantity": "1", "sell_quantity": "0.5", "buy_notional": "101",
                       "sell_notional": "55", "fees": "0.156", "fill_realized_pnl": "4.5",
                       "funding_payments": "0", "settlement_payouts": "0"}.items():
        assert D(group[key]) == D(value)
    selected = activity_report(ledger, "alpha", since=later.observed_at.isoformat())
    assert selected["event_count"] == 2
    assert selected["instruments"][0]["buy_quantity"] == "0"
    earlier = activity_report(ledger, "alpha", until="2026-01-01T08:00:01+08:00")
    assert earlier["event_count"] == 1 and earlier["instruments"][0]["sell_quantity"] == "0"
    assert (ledger.account("alpha").to_state(), ledger.history("alpha")) == before


def test_funding_and_settlement_are_separate_cash_flows_with_unknown_currency():
    report = summarize_events([
        {"sequence": 1, "event_type": "funding", "payload": {
            "quote": {"instrument_id": "perp"}, "result": "-0.0001"}},
        {"sequence": 2, "event_type": "funding", "payload": {
            "quote": {"instrument_id": "perp"}, "result": "0.00002"}},
        {"sequence": 3, "event_type": "settlement", "payload": {
            "instrument_id": "event", "result": "120.25"}},
    ])
    event, perp = report["instruments"]
    assert event["quote_currency"] is None and perp["quote_currency"] is None
    assert event["settlement_payouts"] == "120.25" and event["fill_realized_pnl"] == "0"
    assert perp["funding_payments"] == "-0.00008" and perp["settlement_payouts"] == "0"


def event_fill(**patch):
    return {"sequence": 1, "event_type": "fill", "payload": {
        "instrument": {"quote_currency": "USDT"}, "result": {
            "instrument_id": "spot", "side": "buy", "quantity": "1", "price": "101",
            "fee": "0.101", "realized_pnl": "0", **patch}}}


def test_activity_preserves_precision_beyond_default_decimal_context():
    result = summarize_events([
        event_fill(quantity="2", price="12345678901234567890.1234567890123456789", fee="1"),
        event_fill(quantity="0.00000000000000000001", price="0.00000000000000000001",
                   fee="0.000000000000000000000000000001"),
    ])["instruments"][0]
    assert D(result["buy_notional"]) == D(
        "24691357802469135780.2469135780246913578000000000000000000001",
    )
    assert result["fees"] == "1.000000000000000000000000000001"


@pytest.mark.parametrize("patch", [
    {"price": "NaN"}, {"quantity": "0"}, {"price": "broken"}, {"fee": "-1"},
    {"side": "other"}, {"quantity": 1},
])
def test_activity_rejects_unusable_amounts_instead_of_skipping_events(patch):
    with pytest.raises(TradingError):
        summarize_events([event_fill(**patch)])


def test_activity_rejects_incomplete_events_and_conflicting_currencies():
    with pytest.raises(TradingError, match="incomplete"):
        summarize_events([{"sequence": 1, "event_type": "fill", "payload": {}}])
    other = event_fill()
    other["payload"]["instrument"]["quote_currency"] = "USDC"
    with pytest.raises(TradingError, match="currencies"):
        summarize_events([event_fill(), other])


def test_activity_streams_past_history_page_limits():
    report = summarize_events(event_fill() for _ in range(1005))
    assert report["event_count"] == 1005
    assert D(report["instruments"][0]["buy_quantity"]) == 1005
    assert D(report["instruments"][0]["fees"]) == D("101.505")


def test_cli_activity_is_offline_and_preserves_missing_path(tmp_path, monkeypatch, capsys):
    market = Mock(side_effect=AssertionError("Activity must stay offline"))
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData", market)
    path = tmp_path / "missing" / "paper.db"
    main(["activity", "alpha", "--db", str(path), "--since", "2026-11-01T00:00:00Z"])
    report = json.loads(capsys.readouterr().out)
    assert report["event_count"] == 0 and report["instruments"] == []
    assert report["first_sequence"] is None and not path.parent.exists()
    with pytest.raises(SystemExit) as exc:
        main(["activity", "alpha", "--db", str(path), "--since", "2026-11-01"])
    assert exc.value.code == 2 and "time zone" in capsys.readouterr().err
    market.assert_not_called()
