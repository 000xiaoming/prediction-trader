"""Validated, advisory-only TradingAgents snapshots for the live indicator display.

This module never creates orders, alters probabilities, or reads historical replay
inputs. A snapshot records both source production time and local availability.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_AGE = timedelta(hours=1)
PortfolioRating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
ResearchSymbol = Literal["BTC-USD", "ETH-USD"]


def utc_timestamp(value: datetime | str) -> datetime:
    """Parse an explicit, timezone-aware ISO timestamp and normalize to UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("timestamp must be an aware ISO datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be an aware ISO datetime")
    return value.astimezone(UTC)


class ResearchContext(BaseModel):
    """A source rating, never a five-minute Up/Down probability or trade signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source: Literal["tradingagents"] = "tradingagents"
    symbol: ResearchSymbol = "BTC-USD"
    generated_at: AwareDatetime
    data_as_of: AwareDatetime
    available_at: AwareDatetime
    valid_until: AwareDatetime
    rating: PortfolioRating
    rationale: str = Field(min_length=1, max_length=4096)
    source_run_id: str = Field(min_length=1, max_length=160)
    time_horizon: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("generated_at", "data_as_of", "available_at", "valid_until", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any) -> datetime:
        return utc_timestamp(value)

    @field_validator("rationale", "source_run_id", "time_horizon")
    @classmethod
    def nonblank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text must not be blank")
        return value

    @model_validator(mode="after")
    def chronological(self) -> Self:
        if not self.data_as_of <= self.generated_at <= self.available_at < self.valid_until:
            raise ValueError("timestamps must follow source data, generation, availability, expiry")
        if self.valid_until - self.generated_at > MAX_CONTEXT_AGE:
            raise ValueError("validity must not exceed one hour")
        if self.generated_at - self.data_as_of > MAX_CONTEXT_AGE:
            raise ValueError("source evidence must not precede generation by more than one hour")
        return self


class FileResearchContextProvider:
    """Read a bounded snapshot for a current live display; never for historical replay."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        expected_symbol: str = "BTC-USD",
    ) -> None:
        self.path = Path(path) if path is not None and str(path).strip() else None
        if expected_symbol not in {"BTC-USD", "ETH-USD"}:
            raise ValueError("expected_symbol must be BTC-USD or ETH-USD")
        self.expected_symbol = expected_symbol

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        checked_at = utc_timestamp(now or datetime.now(UTC))
        if self.path is None:
            return {"status": "disabled", "context": None}
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_CONTEXT_BYTES + 1)
            if len(raw) > MAX_CONTEXT_BYTES:
                return {"status": "invalid", "context": None}
            context = ResearchContext.model_validate_json(raw)
        except FileNotFoundError:
            return {"status": "missing", "context": None}
        except (OSError, ValueError):
            # Never expose paths, file contents, credentials, or parser exceptions.
            return {"status": "invalid", "context": None}

        if context.symbol != self.expected_symbol:
            return {"status": "symbol_mismatch", "context": None}
        if max(context.generated_at, context.data_as_of, context.available_at) > checked_at:
            return {"status": "future", "context": None}
        if checked_at >= context.valid_until or checked_at - context.data_as_of > MAX_CONTEXT_AGE:
            return {"status": "stale", "context": None}
        return {"status": "available", "context": context.model_dump(mode="json")}


def write_research_context(path: str | os.PathLike[str], context: ResearchContext) -> None:
    """Atomically publish public advisory data readable by a container with a different UID.

    The snapshot has mode 0644; only nonsecret research fields belong in this file.
    The containing bind-mounted directory must also be traversable by the reader.
    """
    raw = (context.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError("snapshot exceeds the size limit")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.chmod(temporary, 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
