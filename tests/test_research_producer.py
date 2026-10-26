import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from prediction_trader.config import Settings
from prediction_trader.indicator_state import IndicatorStateReader
from prediction_trader.research.producer import ProducerResearchContextProvider

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _run(
    root: Path,
    name: str = "run-1",
    *,
    generated: datetime = NOW,
    evidence_time: bool = True,
    ticker: str = "BTC-USD",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    run = {
        "status": "completed",
        "ticker": ticker,
        "run_id": name,
        "completed_at": generated.isoformat(),
    }
    result = {
        "run_id": name,
        "created_at": generated.isoformat(),
        "expires_at": (generated + timedelta(minutes=15)).isoformat(),
        "rating": "Hold",
        "reason": "Research only",
        "evidence": {"ticker": ticker},
    }
    if evidence_time:
        result["data_as_of"] = (generated - timedelta(seconds=30)).isoformat()
    (directory / "run.json").write_text(json.dumps(run))
    (directory / "result.json").write_text(json.dumps(result))
    return directory


def _provider(root: Path, **kwargs: object) -> ProducerResearchContextProvider:
    return ProducerResearchContextProvider(str(root), clock=lambda: NOW, **kwargs)


def test_missing_evidence_time_is_explicit_not_inferred(tmp_path: Path) -> None:
    _run(tmp_path, evidence_time=False)
    assert _provider(tmp_path).read() == {"status": "missing_evidence_timestamp", "context": None}


def test_valid_source_keeps_its_times_and_real_first_local_availability(tmp_path: Path) -> None:
    _run(tmp_path, generated=NOW - timedelta(minutes=2))
    provider = _provider(tmp_path)
    first = provider.read()
    second = provider.read(NOW + timedelta(minutes=1))
    assert first == second
    assert first["status"] == "available"
    assert first["context"]["available_at"] == "2026-09-19T12:00:00Z"
    assert first["context"]["generated_at"] == "2026-09-19T11:58:00Z"
    assert provider.read(NOW + timedelta(minutes=14))["status"] == "stale"


def test_newer_missing_timestamp_result_blocks_older_valid_result(tmp_path: Path) -> None:
    _run(tmp_path, "old", generated=NOW - timedelta(minutes=3))
    _run(tmp_path, "new", evidence_time=False)
    assert _provider(tmp_path).read()["status"] == "missing_evidence_timestamp"


def test_btc_result_is_not_exposed_to_eth(tmp_path: Path) -> None:
    _run(tmp_path)
    assert _provider(tmp_path, expected_symbol="ETH-USD").read()["status"] == "missing"


@pytest.mark.parametrize("failure", ["running", "bad_json", "oversized", "mismatch", "symlink"])
def test_incomplete_invalid_or_symlink_artifacts_are_ignored(tmp_path: Path, failure: str) -> None:
    directory = _run(tmp_path)
    if failure == "running":
        (directory / "run.json").write_text('{"status":"running"}')
    elif failure == "bad_json":
        (directory / "result.json").write_text("{")
    elif failure == "oversized":
        (directory / "result.json").write_text(" " * 65537)
    elif failure == "mismatch":
        run = json.loads((directory / "run.json").read_text())
        run["run_id"] = "other"
        (directory / "run.json").write_text(json.dumps(run))
    else:
        (directory / "result.json").rename(tmp_path / "secret.json")
        (directory / "result.json").symlink_to(tmp_path / "secret.json")
    assert _provider(tmp_path).read()["context"] is None


def test_symlink_run_directory_and_configured_symlink_root_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _run(outside)
    root = tmp_path / "root"
    root.mkdir()
    (root / "run-1").symlink_to(outside / "run-1", target_is_directory=True)
    assert _provider(root).read()["context"] is None
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    assert _provider(alias).read()["context"] is None


def test_scan_limit_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(tmp_path, "one")
    _run(tmp_path, "two")
    monkeypatch.setattr("prediction_trader.research.producer.MAX_RUN_DIRECTORIES", 1)
    assert _provider(tmp_path).read()["status"] == "scan_limit_exceeded"


def test_cached_scan_does_not_hit_disk_each_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path)
    provider = _provider(tmp_path)
    provider.read()
    scan = Mock(side_effect=AssertionError("Unexpected disk rescan"))
    monkeypatch.setattr(provider, "_scan", scan)
    assert provider.read()["status"] == "available"
    scan.assert_not_called()


def test_reader_keeps_explicit_available_file_priority() -> None:
    repository = Mock()
    connection = repository.engine.connect.return_value.execution_options.return_value
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.mappings.return_value.first.return_value = None
    reader = IndicatorStateReader(
        repository,
        settings=Settings(
            _env_file=None,
            tradingagents_research_dir="/unused",
        ),
    )
    reader._research = Mock()
    reader._research.read.return_value = {"status": "available", "context": {"rating": "Hold"}}
    reader._producer_research = Mock()
    snapshot = reader.read(NOW)
    assert snapshot["research"] == reader._research.read.return_value
    reader._producer_research.read.assert_not_called()


def test_reader_uses_producer_when_explicit_file_not_available() -> None:
    repository = Mock()
    connection = repository.engine.connect.return_value.execution_options.return_value
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.mappings.return_value.first.return_value = None
    reader = IndicatorStateReader(
        repository,
        settings=Settings(
            _env_file=None,
            tradingagents_research_dir="/unused",
        ),
    )
    reader._research = Mock()
    reader._research.read.return_value = {"status": "missing", "context": None}
    reader._producer_research = Mock()
    reader._producer_research.read.return_value = {
        "status": "missing_evidence_timestamp",
        "context": None,
    }
    assert reader.read(NOW)["research"]["status"] == "missing_evidence_timestamp"
