"""Validated SQLite snapshots and recovery to a new, never-overwritten ledger file."""

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any, BinaryIO

from prediction_trader.trading.engine import PaperAccount
from prediction_trader.trading.ledger import EVENT_TYPES
from prediction_trader.trading.models import TradingError

_COLUMNS = {
    "paper_accounts": [
        ("portfolio_id", "TEXT", 0, 1), ("state", "TEXT", 1, 0), ("updated_at", "TEXT", 1, 0),
    ],
    "paper_events": [
        ("sequence", "INTEGER", 0, 1), ("portfolio_id", "TEXT", 1, 0),
        ("event_key", "TEXT", 1, 0), ("event_type", "TEXT", 1, 0),
        ("observed_at", "TEXT", 1, 0), ("payload", "TEXT", 1, 0),
    ],
}
_UNIQUE_KEYS = {
    "paper_accounts": {("portfolio_id",)},
    "paper_events": {("portfolio_id", "event_key")},
}


def _source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise TradingError("Source ledger or backup file does not exist")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _validate(connection: sqlite3.Connection) -> dict[str, int]:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise TradingError("SQLite integrity check failed")
    objects = connection.execute(
        "SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'",
    ).fetchall()
    if ({name for kind, name in objects if kind == "table"} != set(_COLUMNS)
            or any(kind not in {"table", "index"} for kind, _ in objects)):
        raise TradingError("Not a supported paper ledger schema")
    for table, expected in _COLUMNS.items():
        columns = connection.execute(
            'SELECT name,type,"notnull",pk FROM pragma_table_info(?)', (table,),
        ).fetchall()
        if columns != expected:
            raise TradingError("Unsupported paper ledger columns")
        unique_keys = {
            tuple(row[0] for row in connection.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,),
            ))
            for name, in connection.execute(
                'SELECT name FROM pragma_index_list(?) WHERE "unique"=1 AND partial=0', (table,),
            )
        }
        if unique_keys != _UNIQUE_KEYS[table]:
            raise TradingError("Unsupported paper ledger uniqueness constraints")
        references = connection.execute(
            'SELECT "table","from","to",on_update,on_delete,match '
            'FROM pragma_foreign_key_list(?)', (table,),
        ).fetchall()
        expected_references = [] if table == "paper_accounts" else [
            ("paper_accounts", "portfolio_id", "portfolio_id", "NO ACTION", "NO ACTION", "NONE"),
        ]
        if references != expected_references:
            raise TradingError("Unsupported paper ledger account references")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise TradingError("Paper ledger has broken account references")
    if connection.execute(
        "SELECT 1 FROM paper_events e LEFT JOIN paper_accounts a "
        "ON e.portfolio_id=a.portfolio_id WHERE a.portfolio_id IS NULL LIMIT 1",
    ).fetchone():
        raise TradingError("Paper ledger has orphaned events")
    accounts = 0
    for portfolio, encoded in connection.execute("SELECT portfolio_id,state FROM paper_accounts"):
        try:
            state = json.loads(encoded)
            account = PaperAccount.from_state(state)
            if account.portfolio_id != portfolio:
                raise ValueError("portfolio mismatch")
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            raise TradingError("Paper ledger contains an unreadable account state") from exc
        accounts += 1
    events = 0
    for kind, encoded in connection.execute("SELECT event_type,payload FROM paper_events"):
        try:
            payload = json.loads(encoded)
        except (ValueError, TypeError) as exc:
            raise TradingError("Paper ledger contains an invalid event payload") from exc
        if kind not in EVENT_TYPES or not isinstance(payload, dict):
            raise TradingError("Paper ledger contains an unsupported event")
        events += 1
    return {"account_count": accounts, "event_count": events}


def verify_backup(path: str | Path) -> dict[str, Any]:
    """Verify a standalone backup, including loadable accounts and event payloads."""
    source = Path(path)
    with closing(_source(source)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise TradingError("Expected a standalone backup; create one with the backup command")
        counts = _validate(connection)
    with source.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"format": "prediction-paper-sqlite-v1", "verified": True,
            "bytes": source.stat().st_size, "sha256": checksum, **counts}


def _unused_destination(path: Path) -> None:
    # A dangling symlink or leftover SQLite sidecar must not be treated as a fresh target.
    if any(os.path.lexists(str(path) + suffix) for suffix in ("", "-wal", "-shm", "-journal")):
        raise TradingError("Destination or SQLite sidecar already exists; choose a new filename")


def backup_ledger(
    source: str | Path, destination: str | Path, *, timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Capture committed WAL data using SQLite's online backup API, then publish atomically."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise TradingError("Backup timeout must be positive and finite")
    target = Path(destination)
    _unused_destination(target)
    with closing(_source(Path(source))) as connection:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, filename = tempfile.mkstemp(prefix=".paper-backup-", dir=target.parent)
        os.close(descriptor)
        temporary = Path(filename)
        deadline = time.monotonic() + timeout_seconds

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() > deadline:
                raise TradingError("Backup timed out; retry when the ledger is less busy")

        try:
            with closing(sqlite3.connect(temporary)) as snapshot:
                connection.backup(snapshot, pages=256, progress=progress, sleep=0.05)
                snapshot.execute("PRAGMA journal_mode=DELETE")
            metadata = verify_backup(temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            _unused_destination(target)
            # Unlike replace(), link() fails if another process has created the destination.
            os.link(temporary, target)
            return {"operation": "backup", "path": str(target), **metadata}
        finally:
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(temporary) + suffix).unlink(missing_ok=True)


def restore_backup(
    source: str | Path, destination: str | Path, *, timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Restore into a new file; never replace the application's active ledger."""
    verify_backup(source)
    result = backup_ledger(source, destination, timeout_seconds=timeout_seconds)
    return {**result, "operation": "restore"}


def restore_stream(
    source: BinaryIO, destination: str | Path, *, timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Accept a host backup over stdin without changing Docker volume ownership."""
    target = Path(destination)
    _unused_destination(target)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise TradingError("Backup timeout must be positive and finite")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(prefix=".paper-import-", dir=target.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        return restore_backup(temporary, target, timeout_seconds=timeout_seconds)
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
