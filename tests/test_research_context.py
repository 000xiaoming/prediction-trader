import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from prediction_trader.research.context import (
    MAX_CONTEXT_BYTES,
    FileResearchContextProvider,
    ResearchContext,
    write_research_context,
)

NOW = datetime(2026, 9, 19, 6, 0, tzinfo=UTC)


def sample_context(**overrides: object) -> ResearchContext:
    values: dict[str, object] = {
        "generated_at": NOW - timedelta(minutes=5),
        "data_as_of": NOW - timedelta(minutes=10),
        "available_at": NOW - timedelta(minutes=4),
        "valid_until": NOW + timedelta(minutes=10),
        "rating": "Overweight",
        "rationale": "Source portfolio research; not a five-minute price forecast.",
        "source_run_id": "fixture-run",
        "time_horizon": "3-6 months",
    }
    values.update(overrides)
    return ResearchContext.model_validate(values)


def test_provider_disabled_and_missing_are_distinct(tmp_path: Path) -> None:
    assert FileResearchContextProvider().read(NOW) == {"status": "disabled", "context": None}
    assert FileResearchContextProvider("  ").read(NOW)["status"] == "disabled"
    assert FileResearchContextProvider(tmp_path / "absent").read(NOW) == {
        "status": "missing",
        "context": None,
    }


def test_provider_round_trip_preserves_advisory_rating_and_horizon(tmp_path: Path) -> None:
    context = sample_context()
    path = tmp_path / "nested" / "research.json"
    write_research_context(path, context)
    envelope = FileResearchContextProvider(path).read(NOW)
    assert envelope == {"status": "available", "context": context.model_dump(mode="json")}
    assert envelope["context"]["time_horizon"] == "3-6 months"
    assert "yes_probability" not in envelope["context"]


@pytest.mark.parametrize("rating", ["Buy", "Overweight", "Hold", "Underweight", "Sell"])
def test_actual_tradingagents_rating_vocabulary(rating: str) -> None:
    assert sample_context(rating=rating).rating == rating


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "SOL-USD"},
        {"source": "unknown"},
        {"rating": "REVIEW"},
        {"rating": "BUY"},
        {"schema_version": 2},
        {"yes_probability": 0.9},
        {"rationale": " "},
        {"rationale": "x" * 4097},
        {"source_run_id": "x" * 161},
        {"time_horizon": "x" * 161},
        {"generated_at": NOW.replace(tzinfo=None)},
        {"generated_at": NOW.timestamp()},
        {"data_as_of": NOW},
        {"available_at": NOW - timedelta(minutes=6)},
        {"valid_until": NOW - timedelta(minutes=4)},
        {"valid_until": NOW + timedelta(hours=1)},
        {"data_as_of": NOW - timedelta(hours=2)},
    ],
)
def test_schema_rejects_untrusted_or_inconsistent_context(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        sample_context(**changes)


def test_context_is_frozen_and_normalizes_aware_timestamps() -> None:
    context = sample_context(
        generated_at=(NOW - timedelta(minutes=5)).astimezone(timezone(timedelta(hours=8)))
    )
    assert context.generated_at.tzinfo == UTC
    with pytest.raises(ValidationError):
        context.rating = "Sell"


def test_eth_research_is_available_only_to_matching_asset(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    context = sample_context(symbol="ETH-USD")
    write_research_context(path, context)
    assert FileResearchContextProvider(path, expected_symbol="ETH-USD").read(NOW) == {
        "status": "available",
        "context": context.model_dump(mode="json"),
    }
    assert FileResearchContextProvider(path).read(NOW) == {
        "status": "symbol_mismatch",
        "context": None,
    }


def test_legacy_btc_snapshot_cannot_be_used_as_eth_research(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    context = sample_context()
    # Older snapshots used the schema's implicit BTC-USD default.
    path.write_text(context.model_dump_json(exclude={"symbol"}))
    assert FileResearchContextProvider(path).read(NOW)["status"] == "available"
    assert FileResearchContextProvider(path, expected_symbol="ETH-USD").read(NOW) == {
        "status": "symbol_mismatch",
        "context": None,
    }


def test_provider_rejects_unsupported_expected_symbol() -> None:
    with pytest.raises(ValueError, match="BTC-USD or ETH-USD"):
        FileResearchContextProvider(expected_symbol="SOL-USD")


@pytest.mark.parametrize("raw", [b"{not-json secret-token}", b"\xff", b"["])
def test_invalid_file_never_exposes_contents(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "research.json"
    path.write_bytes(raw)
    assert FileResearchContextProvider(path).read(NOW) == {"status": "invalid", "context": None}


def test_provider_rejects_oversized_file_and_directory(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    path.write_bytes(b" " * (MAX_CONTEXT_BYTES + 1))
    assert FileResearchContextProvider(path).read(NOW)["status"] == "invalid"
    assert FileResearchContextProvider(tmp_path).read(NOW)["status"] == "invalid"


def test_expired_context_is_not_available_at_boundary(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    context = sample_context()
    write_research_context(path, context)
    assert FileResearchContextProvider(path).read(context.valid_until) == {
        "status": "stale",
        "context": None,
    }


def test_old_evidence_expires_even_with_unexpired_generation_ttl(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    context = sample_context(
        data_as_of=NOW - timedelta(minutes=61),
        generated_at=NOW - timedelta(minutes=2),
        available_at=NOW - timedelta(minutes=1),
    )
    write_research_context(path, context)
    assert FileResearchContextProvider(path).read(NOW)["status"] == "stale"


@pytest.mark.parametrize("future_source", [False, True])
def test_future_source_or_availability_is_not_visible(
    tmp_path: Path,
    future_source: bool,
) -> None:
    changes: dict[str, object] = {"available_at": NOW + timedelta(seconds=1)}
    if future_source:
        changes["data_as_of"] = NOW + timedelta(seconds=1)
        changes["generated_at"] = NOW + timedelta(seconds=1)
    path = tmp_path / "research.json"
    write_research_context(path, sample_context(**changes))
    assert FileResearchContextProvider(path).read(NOW) == {"status": "future", "context": None}


def test_failed_atomic_publication_keeps_old_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    original = sample_context()
    write_research_context(path, original)
    previous = path.read_bytes()
    with (
        patch("prediction_trader.research.context.os.replace", side_effect=PermissionError),
        pytest.raises(PermissionError),
    ):
        write_research_context(path, sample_context(rating="Sell"))
    assert path.read_bytes() == previous
    assert sorted(item.name for item in tmp_path.iterdir()) == ["research.json"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX bind mount permissions")
def test_atomic_publication_is_readable_by_a_different_container_uid(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    write_research_context(path, sample_context())
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.chmod(0o600)
    write_research_context(path, sample_context(rating="Sell"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
