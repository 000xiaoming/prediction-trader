import csv
import io
import json
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from prediction_trader.trading import history_export
from prediction_trader.trading.cli import main
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import TradingError

EVENT = {"sequence": 1, "event_type": "funding", "observed_at": "2026-01-01T00:00:00+00:00",
         "payload": {"result": "-0.000000000000000001", "note": "模拟记录"}}


@pytest.mark.parametrize("format_name", ["csv", "jsonl"])
def test_atomic_exports_preserve_precision_unicode_and_private_permissions(
    tmp_path: Path, format_name: str,
) -> None:
    target = tmp_path / "exports" / ("events." + format_name)
    result = history_export.export_file([EVENT], "alpha", target, format_name=format_name)
    assert result["event_count"] == 1 and result["bytes"] == target.stat().st_size
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    content = target.read_text()
    if format_name == "csv":
        row = next(csv.DictReader(io.StringIO(content)))
        assert row["result"] == EVENT["payload"]["result"]
        assert json.loads(row["payload_json"]) == EVENT["payload"]
    else:
        assert json.loads(content) == {"portfolio_id": "alpha", **EVENT}
    assert not list(target.parent.glob(".paper-export-*"))


def test_failed_stream_leaves_no_partial_export(tmp_path: Path) -> None:
    def broken() -> Iterator[dict]:
        yield EVENT
        raise OSError("read failed midway")
    target = tmp_path / "events.csv"
    with pytest.raises(OSError, match="midway"):
        history_export.export_file(broken(), "alpha", target)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_or_dangling_paths_are_never_replaced(tmp_path: Path, symlink: bool) -> None:
    target = tmp_path / "events.csv"
    if symlink:
        target.symlink_to(tmp_path / "missing.csv")
    else:
        target.write_text("original")
    with pytest.raises(TradingError, match="already exists"):
        history_export.export_file([EVENT], "alpha", target)
    assert target.is_symlink() if symlink else target.read_text() == "original"


def test_another_export_winning_the_race_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = history_export.os.link
    def race(source: Path, target: Path) -> None:
        target.write_text("other export")
        link(source, target)
    monkeypatch.setattr(history_export.os, "link", race)
    target = tmp_path / "events.csv"
    with pytest.raises(FileExistsError):
        history_export.export_file([EVENT], "alpha", target)
    assert target.read_text() == "other export" and not list(tmp_path.glob(".paper-export-*"))


def test_cli_export_refuses_the_source_ledger_and_reports_success_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    before = ledger.path.read_bytes()
    with pytest.raises(SystemExit) as failure:
        main(["export", "alpha", "--db", str(ledger.path), "--output", str(ledger.path)])
    assert failure.value.code == 2 and ledger.path.read_bytes() == before
    capsys.readouterr()
    target = tmp_path / "empty.csv"
    main(["export", "alpha", "--db", str(ledger.path), "--output", str(target)])
    assert json.loads(capsys.readouterr().out)["event_count"] == 0
    assert target.read_text().startswith("sequence,portfolio_id,")
