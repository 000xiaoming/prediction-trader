import io
import json
import sqlite3
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from prediction_trader.trading import backup
from prediction_trader.trading.catalog import get_instrument
from prediction_trader.trading.cli import main
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import PaperOrder, Quote, TradingError

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def populated(tmp_path: Path) -> tuple[SQLitePaperLedger, dict, dict]:
    ledger = SQLitePaperLedger(tmp_path / "active.db")
    orders, quotes = {}, {}
    for portfolio, instrument in (("spot", "binance_btc_spot"), ("perp", "binance_btc_perpetual")):
        spec = get_instrument(instrument)
        order = PaperOrder("first", instrument, portfolio, "buy", Decimal("1"), Decimal("10"))
        quote = Quote(instrument, portfolio, NOW, Decimal("100"), Decimal("101"),
                      Decimal("10"), Decimal("10"), mark_price=Decimal("100"))
        ledger.execute(spec, order, quote, now=NOW)
        orders[portfolio], quotes[portfolio] = order, quote
    ledger.funding("fund-1", quotes["perp"], Decimal("0.0001"), now=NOW)
    ledger.mark(quotes["spot"], now=NOW)
    return ledger, orders, quotes


def test_backup_restore_preserves_accounts_events_and_idempotency(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path,
) -> None:
    ledger, orders, quotes = populated
    target = tmp_path / "backup folder" / "snapshot.sqlite3"
    result = backup.backup_ledger(ledger.path, target)
    assert result["verified"] and result["account_count"] == 2 and result["event_count"] == 4
    assert len(result["sha256"]) == 64
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not Path(str(target) + "-wal").exists()
    restored_path = tmp_path / "recovered" / "paper.sqlite3"
    assert backup.restore_backup(target, restored_path)["operation"] == "restore"
    restored = SQLitePaperLedger(restored_path)
    for portfolio in ("spot", "perp"):
        assert restored.account(portfolio).to_state() == ledger.account(portfolio).to_state()
        assert restored.history(portfolio) == ledger.history(portfolio)
        restored.execute(get_instrument(orders[portfolio].instrument_id),
                         orders[portfolio], quotes[portfolio], now=NOW)
        assert restored.history(portfolio) == ledger.history(portfolio)
    restored.funding("fund-1", quotes["perp"], Decimal("0.0001"), now=NOW)
    assert restored.account("perp").to_state() == ledger.account("perp").to_state()
    assert restored.history("perp") == ledger.history("perp")


def test_online_backup_includes_committed_wal_but_not_uncommitted_writes(tmp_path: Path) -> None:
    path = tmp_path / "live.db"
    ledger = SQLitePaperLedger(path)
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        spec = get_instrument("binance_btc_spot")
        order = PaperOrder(
            "wal-order", spec.instrument_id, "spot", "buy", Decimal("1"), Decimal("0"),
        )
        quote = Quote(spec.instrument_id, "spot", NOW, Decimal("100"), Decimal("101"),
                      Decimal("10"), Decimal("10"))
        ledger.execute(spec, order, quote, now=NOW)
        expected = ledger.account("spot").to_state()
        assert Path(str(path) + "-wal").stat().st_size > 0
        writer.execute("UPDATE paper_accounts SET state='uncommitted and invalid'")
        target = tmp_path / "wal-snapshot.db"
        backup.backup_ledger(path, target)
        assert SQLitePaperLedger(target, read_only=True).account("spot").to_state() == expected
        writer.rollback()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_backup_and_restore_refuse_existing_targets_and_sidecars(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path, suffix: str,
) -> None:
    ledger, _, _ = populated
    snapshot = tmp_path / "snapshot.db"
    backup.backup_ledger(ledger.path, snapshot)
    target = tmp_path / "existing.db"
    occupied = Path(str(target) + suffix)
    occupied.write_bytes(b"keep existing data")
    for operation, source in (
        (backup.backup_ledger, ledger.path), (backup.restore_backup, snapshot),
    ):
        with pytest.raises(TradingError, match="already exists"):
            operation(source, target)
        assert occupied.read_bytes() == b"keep existing data"


def test_dangling_symlink_is_never_replaced(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path,
) -> None:
    ledger, _, _ = populated
    target = tmp_path / "link.db"
    target.symlink_to(tmp_path / "missing.db")
    with pytest.raises(TradingError, match="already exists"):
        backup.backup_ledger(ledger.path, target)
    assert target.is_symlink()


def test_destination_created_during_backup_is_preserved(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _, _ = populated
    target = tmp_path / "raced.db"
    original = backup.os.link
    def race(source: Path, destination: Path) -> None:
        destination.write_bytes(b"another writer")
        original(source, destination)
    monkeypatch.setattr(backup.os, "link", race)
    with pytest.raises(FileExistsError):
        backup.backup_ledger(ledger.path, target)
    assert target.read_bytes() == b"another writer"
    assert not list(tmp_path.glob(".paper-backup-*"))


def test_interrupted_backup_does_not_publish_a_partial_file(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _, _ = populated
    ticks = iter((0, 2))
    monkeypatch.setattr(backup.time, "monotonic", lambda: next(ticks))
    target = tmp_path / "partial.db"
    with pytest.raises(TradingError, match="timed out"):
        backup.backup_ledger(ledger.path, target, timeout_seconds=1)
    assert not target.exists()
    assert not list(tmp_path.glob(".paper-backup-*"))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_rejected_before_creating_files(tmp_path: Path, value: float) -> None:
    with pytest.raises(TradingError, match="positive and finite"):
        backup.backup_ledger(tmp_path / "source", tmp_path / "target", timeout_seconds=value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("corruption", ["json", "account", "event", "schema", "orphan"])
def test_verify_and_restore_reject_damaged_or_unrelated_backups(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path, corruption: str,
) -> None:
    ledger, _, _ = populated
    snapshot = tmp_path / "invalid.db"
    backup.backup_ledger(ledger.path, snapshot)
    with sqlite3.connect(snapshot) as connection:
        statement = {
            "json": "UPDATE paper_events SET payload='{'",
            "account": "UPDATE paper_accounts SET state='{}'",
            "event": "UPDATE paper_events SET event_type='unknown'",
            "schema": "CREATE TABLE unrelated(value TEXT)",
            "orphan": "DELETE FROM paper_accounts",
        }[corruption]
        connection.execute(statement)
    for action in (lambda: backup.verify_backup(snapshot),
                   lambda: backup.restore_backup(snapshot, tmp_path / "restored.db")):
        with pytest.raises(TradingError):
            action()
    assert not (tmp_path / "restored.db").exists()


def test_missing_invalid_and_empty_sources(tmp_path: Path) -> None:
    with pytest.raises(TradingError, match="does not exist"):
        backup.backup_ledger(tmp_path / "missing", tmp_path / "backup")
    invalid = tmp_path / "not-sqlite"
    invalid.write_text("not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        backup.verify_backup(invalid)
    empty = SQLitePaperLedger(tmp_path / "empty.db")
    result = backup.backup_ledger(empty.path, tmp_path / "empty-backup.db")
    assert result["account_count"] == result["event_count"] == 0


@pytest.mark.parametrize("removed", [
    ", UNIQUE(portfolio_id, event_key)", " REFERENCES paper_accounts(portfolio_id)",
    " PRIMARY KEY AUTOINCREMENT", " NOT NULL",
])
def test_restore_rejects_ledger_tables_missing_required_constraints(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path, removed: str,
) -> None:
    ledger, _, _ = populated
    snapshot = tmp_path / "missing-constraint.db"
    backup.backup_ledger(ledger.path, snapshot)
    schema = (
        "CREATE TABLE replacement(sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "portfolio_id TEXT NOT NULL REFERENCES paper_accounts(portfolio_id), "
        "event_key TEXT NOT NULL, event_type TEXT NOT NULL, observed_at TEXT NOT NULL, "
        "payload TEXT NOT NULL, UNIQUE(portfolio_id, event_key))"
    ).replace(removed, "")
    with sqlite3.connect(snapshot) as connection:
        connection.execute(schema)
        connection.execute("INSERT INTO replacement SELECT * FROM paper_events")
        connection.execute("DROP TABLE paper_events")
        connection.execute("ALTER TABLE replacement RENAME TO paper_events")
    for action in (lambda: backup.verify_backup(snapshot),
                   lambda: backup.restore_backup(snapshot, tmp_path / "restored.db")):
        with pytest.raises(TradingError, match="Unsupported paper ledger"):
            action()
    assert not (tmp_path / "restored.db").exists()


def test_cli_backup_verify_restore_stays_offline(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    ledger, _, _ = populated
    market = Mock(side_effect=AssertionError("Backup must not access market data"))
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData", market)
    target = tmp_path / "cli-backup.db"
    main(["backup", str(target), "--db", str(ledger.path)])
    created = json.loads(capsys.readouterr().out)
    main(["verify-backup", str(target)])
    assert json.loads(capsys.readouterr().out)["sha256"] == created["sha256"]
    main(["restore", str(target), "--output", str(tmp_path / "cli-restored.db")])
    assert json.loads(capsys.readouterr().out)["event_count"] == 4
    with pytest.raises(SystemExit) as failure:
        main(["restore", str(target), "--output", str(ledger.path)])
    assert failure.value.code == 2
    market.assert_not_called()


def test_restore_host_backup_via_stdin_preserves_state(
    populated: tuple[SQLitePaperLedger, dict, dict], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    ledger, _, _ = populated
    snapshot = tmp_path / "host-backup.db"
    backup.backup_ledger(ledger.path, snapshot)
    stream = io.TextIOWrapper(io.BytesIO(snapshot.read_bytes()))
    monkeypatch.setattr("sys.stdin", stream)
    target = tmp_path / "volume" / "restored.db"
    main(["restore", "-", "--output", str(target)])
    assert json.loads(capsys.readouterr().out)["event_count"] == 4
    assert SQLitePaperLedger(target, read_only=True).account("spot").to_state() == (
        ledger.account("spot").to_state()
    )
    assert not list(target.parent.glob(".paper-*-*"))


def test_truncated_import_never_publishes_a_restored_database(tmp_path: Path) -> None:
    target = tmp_path / "volume" / "restored.db"
    with pytest.raises((sqlite3.DatabaseError, TradingError)):
        backup.restore_stream(io.BytesIO(b"truncated backup"), target)
    assert not target.exists()
    assert not list(target.parent.iterdir())
