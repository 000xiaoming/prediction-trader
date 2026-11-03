import sqlite3
from contextlib import contextmanager

import pytest
from test_paper_history import NOW, fill

from prediction_trader.trading import ledger as module
from prediction_trader.trading.activity import activity_report
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import TradingError


def test_activity_limits_return_complete_totals_or_an_error(tmp_path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    fill(ledger, "one")
    fill(ledger, "two")
    reader = SQLitePaperLedger(ledger.path, read_only=True)
    assert activity_report(reader, "alpha", max_events=2)["event_count"] == 2
    fill(ledger, "three")
    with pytest.raises(TradingError, match="exceeds 2 events"):
        activity_report(reader, "alpha", max_events=2)
    assert activity_report(reader, "alpha")["event_count"] == 3


@pytest.mark.parametrize("options", [
    {"max_events": 0}, {"max_events": -1}, {"max_events": True}, {"max_events": 1.5},
    {"timeout_seconds": 0}, {"timeout_seconds": -1},
    {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")},
])
def test_history_limits_validate_before_missing_path_access(tmp_path, options):
    reader = SQLitePaperLedger(tmp_path / "missing" / "paper.db", read_only=True)
    with pytest.raises(TradingError):
        list(reader.iter_history("alpha", **options))
    assert not reader.path.parent.exists()


def test_timeout_interrupts_a_time_filtered_scan_even_without_matching_rows(tmp_path, monkeypatch):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    fill(ledger, "first")
    with sqlite3.connect(ledger.path) as connection:
        connection.executemany(
            "INSERT INTO paper_events(portfolio_id,event_key,event_type,observed_at,payload) "
            "VALUES('alpha',?,'mark',?,'{}')",
            [(f"mark:{i}", NOW.isoformat()) for i in range(3000)],
        )
    clock = [0.0]
    original = module._timestamp_us
    def timestamp(value):
        clock[0] += 0.01
        return original(value)
    monkeypatch.setattr(module, "_timestamp_us", timestamp)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    reader = SQLitePaperLedger(ledger.path, read_only=True)
    with pytest.raises(TradingError, match="timed out"):
        activity_report(reader, "alpha", since="2027-01-01T00:00:00Z", timeout_seconds=0.05)
    assert clock[0] < 30  # The database scan was interrupted before visiting all rows.
    assert reader.status()["event_count"] == 3001


def test_failed_activity_releases_its_read_transaction(tmp_path, monkeypatch):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    fill(ledger, "first")
    with sqlite3.connect(ledger.path) as writer:
        writer.execute("UPDATE paper_events SET payload='{}'")
    reader = SQLitePaperLedger(ledger.path, read_only=True)
    original = reader._connect
    closed = []
    @contextmanager
    def track():
        try:
            with original() as connection:
                yield connection
        finally:
            closed.append(True)
    monkeypatch.setattr(reader, "_connect", track)
    with pytest.raises(TradingError, match="incomplete"):
        activity_report(reader, "alpha", timeout_seconds=1)
    assert closed == [True]
    with sqlite3.connect(ledger.path) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
