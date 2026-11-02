import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_paper_preview import inputs

from prediction_trader.trading import backup
from prediction_trader.trading.cli import main
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import TradingError


def test_check_live_wal_ignores_uncommitted_writes_and_preserves_state(tmp_path: Path):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    spec, order, quote = inputs()
    with sqlite3.connect(ledger.path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        ledger.execute(spec, order, quote, now=quote.observed_at)
        before = ledger.account("alpha").to_state(), ledger.history("alpha")
        assert Path(str(ledger.path) + "-wal").stat().st_size > 0
        writer.execute("UPDATE paper_accounts SET state='uncommitted invalid data'")
        result = backup.check_ledger(ledger.path)
        assert result == {"operation": "check", "verified": True, "journal_mode": "wal",
                          "account_count": 1, "event_count": 1}
        writer.rollback()
        assert (ledger.account("alpha").to_state(), ledger.history("alpha")) == before


def test_check_uses_one_snapshot_while_new_events_commit(tmp_path, monkeypatch):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    spec, order, quote = inputs()
    ledger.execute(spec, order, quote, now=quote.observed_at)
    original = backup._source
    wrote = False
    def open_source(path):
        connection = original(path)
        def trace(statement):
            nonlocal wrote
            if "SELECT type,name" in statement and not wrote:
                wrote = True
                ledger.execute(spec, replace(order, client_order_id="concurrent"), quote,
                               now=quote.observed_at)
        connection.set_trace_callback(trace)
        return connection
    monkeypatch.setattr(backup, "_source", open_source)
    assert backup.check_ledger(ledger.path)["event_count"] == 1
    assert wrote and ledger.status()["event_count"] == 2


@pytest.mark.parametrize("table,column", [
    ("paper_accounts", "updated_at"), ("paper_events", "observed_at"),
])
@pytest.mark.parametrize("invalid", ["2026-01-01T00:00:00", "not-a-date"])
def test_check_and_backup_reject_unusable_event_times(tmp_path, table, column, invalid):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    spec, order, quote = inputs()
    ledger.execute(spec, order, quote, now=quote.observed_at)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (invalid,))
    with pytest.raises(TradingError, match="timestamp"):
        backup.check_ledger(ledger.path)
    target = tmp_path / "snapshot.db"
    with pytest.raises(TradingError, match="timestamp"):
        backup.backup_ledger(ledger.path, target)
    assert not target.exists()


def test_cli_check_missing_files_fail_without_creation(tmp_path, monkeypatch, capsys):
    market = Mock(side_effect=AssertionError("Unexpected market request"))
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData", market)
    missing = tmp_path / "missing" / "paper.db"
    with pytest.raises(SystemExit) as exc:
        main(["check", "--db", str(missing)])
    assert exc.value.code == 2 and not missing.parent.exists()
    assert "does not exist" in capsys.readouterr().err
    empty = SQLitePaperLedger(tmp_path / "empty.db")
    main(["check", "--db", str(empty.path)])
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] and result["event_count"] == result["account_count"] == 0
    market.assert_not_called()


def test_check_rejects_corrupt_accounts_and_foreign_databases(tmp_path):
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(TradingError, match="schema"):
        backup.check_ledger(path)
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    spec, order, quote = inputs()
    ledger.execute(spec, order, quote, now=quote.observed_at)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("UPDATE paper_accounts SET state='{}'")
    with pytest.raises(TradingError, match="unreadable account"):
        backup.check_ledger(ledger.path)
