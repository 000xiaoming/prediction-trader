import hashlib
import json
import stat

import pytest
from test_paper_history import fill
from test_paper_retries import recorded

from prediction_trader.trading import history_export
from prediction_trader.trading.activity import activity_report
from prediction_trader.trading.cli import main
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import TradingError


def test_activity_file_preserves_report_scope_and_precision(tmp_path, capsys):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    fill(ledger, "first")
    before = recorded(ledger)
    report = activity_report(ledger, "alpha", since="2025-12-31T16:00:00-08:00")
    target = tmp_path / "exports" / "report.json"
    main(["activity", "alpha", "--db", str(ledger.path), "--since", report["since"],
          "--output", str(target)])
    metadata = json.loads(capsys.readouterr().out)
    assert json.loads(target.read_text()) == report
    assert metadata["report_type"] == "paper_activity" and metadata["event_count"] == 1
    assert metadata["bytes"] == target.stat().st_size
    assert metadata["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert recorded(ledger) == before and not list(target.parent.glob(".paper-export-*"))


def test_activity_export_does_not_round_decimal_strings_or_escape_unicode(tmp_path):
    target = tmp_path / "precise.json"
    report = {"event_count": 1, "note": "模拟汇总", "fees": "0.000000000000000001"}
    history_export.export_activity(report, target)
    assert json.loads(target.read_text()) == report
    assert "模拟汇总" in target.read_text()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_activity_export_preserves_occupied_paths(tmp_path, kind):
    target = tmp_path / "occupied"
    if kind == "file":
        target.write_text("original")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")
    with pytest.raises(TradingError, match="already exists"):
        history_export.export_activity({"event_count": 0}, target)
    if kind == "file":
        assert target.read_text() == "original"
    elif kind == "directory":
        assert not list(target.iterdir())
    else:
        assert target.is_symlink()


def test_activity_export_failure_does_not_publish_partial_json(tmp_path):
    target = tmp_path / "broken.json"
    with pytest.raises(ValueError):
        history_export.export_activity({"event_count": 0, "invalid": float("nan")}, target)
    assert not list(tmp_path.iterdir())


def test_activity_export_cannot_replace_a_racing_writer(tmp_path, monkeypatch):
    original = history_export.os.link
    target = tmp_path / "report.json"
    def race(source, destination):
        target.write_text("other writer")
        original(source, destination)
    monkeypatch.setattr(history_export.os, "link", race)
    with pytest.raises(FileExistsError):
        history_export.export_activity({"event_count": 0}, target)
    assert target.read_text() == "other writer" and not list(tmp_path.glob(".paper-export-*"))


def test_activity_output_handles_empty_history_without_creating_a_ledger(tmp_path, capsys):
    ledger = tmp_path / "missing" / "paper.db"
    target = tmp_path / "empty.json"
    main(["activity", "alpha", "--db", str(ledger), "--output", str(target)])
    assert json.loads(capsys.readouterr().out)["event_count"] == 0
    assert json.loads(target.read_text())["instruments"] == []
    assert not ledger.parent.exists()


def test_activity_export_refuses_source_database(tmp_path, capsys):
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    before = ledger.path.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(["activity", "alpha", "--db", str(ledger.path), "--output", str(ledger.path)])
    assert exc.value.code == 2 and "already exists" in capsys.readouterr().err
    assert ledger.path.read_bytes() == before


def test_invalid_activity_range_does_not_publish_a_report(tmp_path, capsys):
    target = tmp_path / "report.json"
    with pytest.raises(SystemExit):
        main(["activity", "alpha", "--db", str(tmp_path / "missing.db"),
              "--since", "2026-11-01", "--output", str(target)])
    assert "time zone" in capsys.readouterr().err and not target.exists()
