import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from test_trading_engine import NOW, D, gamma, order, quote, resolved, spec

from prediction_trader.trading.catalog import event_instruments
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import InstrumentKind, TradingError


def recorded(ledger):
    with sqlite3.connect(ledger.path) as connection:
        return (
            connection.execute("SELECT * FROM paper_accounts ORDER BY portfolio_id").fetchall(),
            connection.execute("SELECT * FROM paper_events ORDER BY sequence").fetchall(),
            connection.execute("SELECT * FROM sqlite_sequence").fetchall(),
        )


@pytest.mark.parametrize("kind", ["fill", "mark", "funding", "settlement"])
def test_event_retries_preserve_account_timestamps_history_and_sequence(tmp_path, kind):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    instrument = event_instruments(gamma())[0] if kind == "settlement" else spec(
        InstrumentKind.PERPETUAL,
    )
    current_quote = quote(instrument, price="0.40" if kind == "settlement" else "100")
    request = order(instrument, quantity="10" if kind == "settlement" else "1")
    ledger.execute(instrument, request, current_quote, now=NOW)
    at = NOW + timedelta(days=1) if kind == "settlement" else NOW
    def act(when):
        if kind == "fill":
            return ledger.execute(instrument, request, current_quote, now=when)[0]
        if kind == "mark":
            return ledger.mark(current_quote, now=when)
        if kind == "funding":
            return ledger.funding("fund-1", current_quote, D("0.0001"), now=when)[0]
        return ledger.settle("alpha", instrument.instrument_id, resolved(), now=when)[0]
    first = act(at)
    before = recorded(ledger)
    again = act(at + timedelta(seconds=1))
    assert recorded(ledger) == before
    if kind in {"fill", "funding"}:
        assert again == first
    elif kind == "settlement":
        assert again == 0


def test_concurrent_retries_reserve_only_one_event_sequence(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    instrument = spec()
    with ThreadPoolExecutor(max_workers=4) as pool:
        fills = list(pool.map(lambda _: ledger.execute(
            instrument, order(instrument), quote(instrument), now=NOW,
        )[0], range(8)))
    assert all(fill == fills[0] for fill in fills)
    ledger.execute(instrument, order(instrument, key="next"), quote(instrument), now=NOW)
    assert [event["sequence"] for event in ledger.history("alpha")] == [2, 1]


def test_conflicting_retry_still_rejects_without_altering_original_event(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    instrument = spec()
    ledger.execute(instrument, order(instrument), quote(instrument), now=NOW)
    before = recorded(ledger)
    with pytest.raises(TradingError, match="Idempotency conflict"):
        ledger.execute(instrument, replace(order(instrument), limit_price=D("101")),
                       quote(instrument), now=NOW)
    assert recorded(ledger) == before


def test_retry_of_legacy_order_does_not_rewrite_its_state(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    instrument = spec()
    ledger.execute(instrument, order(instrument), quote(instrument), now=NOW)
    state = ledger.account("alpha").to_state()
    del state["orders"]["first"]["order"]["limit_price"]
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("UPDATE paper_accounts SET state=?", (json.dumps(state),))
    before = recorded(ledger)
    ledger.execute(instrument, order(instrument), quote(instrument), now=NOW + timedelta(hours=1))
    assert recorded(ledger) == before


def test_existing_event_cannot_hide_an_account_change_in_a_damaged_ledger(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    instrument = spec()
    ledger.execute(instrument, order(instrument), quote(instrument), now=NOW)
    state = ledger.account("alpha").to_state()
    state["orders"].clear()
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("UPDATE paper_accounts SET state=?", (json.dumps(state),))
    before = recorded(ledger)
    with pytest.raises(TradingError, match="Recorded event conflicts"):
        ledger.execute(instrument, order(instrument), quote(instrument), now=NOW)
    assert recorded(ledger) == before
