"""Export saved TradingAgents full_states JSON as a current advisory snapshot.

No TradingAgents installation, network request, or LLM call is needed. The input
is the flat JSON written by TradingAgentsGraph._log_state, whose
final_trade_decision is PortfolioDecision rendered as canonical Markdown.
The source currently records a trade_date but no production timestamp: supply
--generated-at with the actual run completion time, never the time of re-export.
--data-as-of is the operator-attested latest timestamp covered by source evidence.
--source-timezone names the producer calendar used for its trade_date (default UTC);
set Asia/Singapore for a producer running on Singapore local time. The exporter
never infers that calendar from its own host timezone.
This bridge is for a live research display, not a historical replay input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from prediction_trader.research.context import (
    ResearchContext,
    utc_timestamp,
    write_research_context,
)

MAX_SOURCE_BYTES = 16 * 1024 * 1024
_SECTION = re.compile(
    r"^\*\*(Rating|Executive Summary|Investment Thesis|Price Target|Time Horizon)"
    r"\*\*:[ \t]*(.*)$",
    re.MULTILINE,
)
_EXPECTED_SECTIONS = [
    "Rating",
    "Executive Summary",
    "Investment Thesis",
    "Price Target",
    "Time Horizon",
]


def _decision_sections(decision: Any) -> dict[str, str]:
    if not isinstance(decision, str):
        raise ValueError("final_trade_decision must be canonical PortfolioDecision Markdown")
    matches = list(_SECTION.finditer(decision))
    if [match.group(1) for match in matches] != _EXPECTED_SECTIONS:
        raise ValueError(
            "final_trade_decision must contain the canonical PortfolioDecision sections"
        )
    if decision[: matches[0].start()].strip():
        raise ValueError("unexpected content before the final decision")
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(decision)
        sections[match.group(1)] = decision[match.start(2) : end].strip()
    if not sections["Executive Summary"] or not sections["Investment Thesis"]:
        raise ValueError("final decision must include a summary and investment thesis")
    return sections


def context_from_full_state(
    state: dict[str, Any],
    *,
    data_as_of: datetime | str,
    generated_at: datetime | str | None,
    available_at: datetime,
    symbol: str = "BTC-USD",
    ttl_seconds: int = 900,
    source_timezone: str = "UTC",
    source_run_id: str,
) -> ResearchContext:
    """Convert the source's exact rendered decision, retaining original timestamps."""
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("ttl_seconds must be an integer between 1 and 3600")
    if not 1 <= ttl_seconds <= 3600:
        raise ValueError("ttl_seconds must be between 1 and 3600")
    if symbol not in {"BTC-USD", "ETH-USD"}:
        raise ValueError("symbol must be BTC-USD or ETH-USD")
    if state.get("company_of_interest") != symbol:
        raise ValueError(f"source company_of_interest must be {symbol}")
    raw_date = state.get("trade_date")
    if not isinstance(raw_date, str):
        raise ValueError("source trade_date must be a canonical ISO date")
    try:
        trade_date = date.fromisoformat(raw_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("source trade_date must be a canonical ISO date") from exc
    if raw_date != trade_date.isoformat():
        raise ValueError("source trade_date must be a canonical ISO date")

    recorded_at = state.get("generated_at")
    if recorded_at is not None:
        production = utc_timestamp(recorded_at)
        if generated_at is not None and utc_timestamp(generated_at) != production:
            raise ValueError("--generated-at must match the recorded source timestamp")
    elif generated_at is not None:
        production = utc_timestamp(generated_at)
    else:
        raise ValueError("source has no generated_at; supply the actual completion --generated-at")
    try:
        producer_zone = ZoneInfo(source_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("source_timezone must be an available IANA timezone") from exc
    if production.astimezone(producer_zone).date() != trade_date:
        raise ValueError(
            "generated_at date in source_timezone must match source trade_date; "
            "do not redate reports"
        )
    coverage = utc_timestamp(data_as_of)
    availability = utc_timestamp(available_at)
    sections = _decision_sections(state.get("final_trade_decision"))
    horizon = sections["Time Horizon"]
    try:
        context = ResearchContext.model_validate(
            {
                "symbol": symbol,
                "generated_at": production,
                "data_as_of": coverage,
                "available_at": availability,
                "valid_until": production + timedelta(seconds=ttl_seconds),
                "rating": sections["Rating"],
                "rationale": sections["Executive Summary"],
                "time_horizon": None if horizon == "not provided" else horizon,
                "source_run_id": source_run_id,
            }
        )
    except ValidationError as exc:
        raise ValueError(
            "invalid or expired source timestamps, rating, or decision fields"
        ) from exc
    if availability - coverage > timedelta(hours=1):
        raise ValueError("source evidence is older than one hour")
    return context


def export_full_state(
    source: str | Path,
    output: str | Path,
    *,
    data_as_of: datetime | str,
    generated_at: datetime | str | None = None,
    symbol: str = "BTC-USD",
    ttl_seconds: int = 900,
    source_timezone: str = "UTC",
    source_run_id: str | None = None,
    now: datetime | None = None,
) -> ResearchContext:
    """Validate a saved run and atomically export it, without refreshing its age."""
    with Path(source).open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source exceeds the size limit")
    try:
        state = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("source is not valid JSON") from exc
    if not isinstance(state, dict):
        raise ValueError("source must be a full_states JSON object")
    context = context_from_full_state(
        state,
        data_as_of=data_as_of,
        generated_at=generated_at,
        available_at=now or datetime.now(UTC),
        symbol=symbol,
        ttl_seconds=ttl_seconds,
        source_timezone=source_timezone,
        source_run_id=(
            source_run_id
            if source_run_id is not None
            else f"sha256:{hashlib.sha256(raw).hexdigest()}"
        ),
    )
    write_research_context(output, context)
    return context


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Export a saved BTC-USD or ETH-USD TradingAgents run for advisory display.",
        epilog=(
            "Timestamps must be timezone-aware ISO values. TTL starts at source generation, "
            "never export time. Expired, REVIEW, malformed, and wrong-symbol reports are rejected. "
            "No network, LLM, order execution, probability fusion, or historical replay."
        ),
    )
    parser.add_argument("source", type=Path, help="TradingAgents full_states_log_YYYY-MM-DD.json")
    parser.add_argument("--output", type=Path, required=True, help="atomic snapshot destination")
    parser.add_argument(
        "--symbol",
        choices=("BTC-USD", "ETH-USD"),
        default="BTC-USD",
        help="exact requested source ticker (default BTC-USD); must match the active profile",
    )
    parser.add_argument(
        "--data-as-of",
        required=True,
        help="actual latest source evidence timestamp; must be no older than one hour",
    )
    parser.add_argument(
        "--generated-at",
        help="actual source run completion time; required unless recorded in source generated_at",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=900,
        help="validity from source generation: 1..3600 seconds (default 900)",
    )
    parser.add_argument(
        "--source-timezone",
        default="UTC",
        help="producer IANA timezone for trade_date, e.g. Asia/Singapore (default UTC)",
    )
    parser.add_argument(
        "--source-run-id", help="optional run identifier; defaults to source SHA-256"
    )
    arguments = parser.parse_args(argv)
    try:
        context = export_full_state(
            arguments.source,
            arguments.output,
            data_as_of=arguments.data_as_of,
            generated_at=arguments.generated_at,
            symbol=arguments.symbol,
            ttl_seconds=arguments.ttl_seconds,
            source_timezone=arguments.source_timezone,
            source_run_id=arguments.source_run_id,
        )
    except OSError:
        parser.error("could not read the source or publish the snapshot")
    except (ValueError, RecursionError) as exc:
        # ValueError messages above are fixed descriptions, never raw report content.
        message = str(exc) if isinstance(exc, ValueError) else "source JSON is too deeply nested"
        parser.error(message)
    else:
        print(json.dumps({"status": "exported", "source_run_id": context.source_run_id}))


if __name__ == "__main__":
    main()
