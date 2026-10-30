"""Portable paper-event exports; preserve decimal strings and the complete original payload."""

import csv
import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TextIO

from prediction_trader.trading.models import TradingError

CSV_FIELDS = (
    "sequence", "portfolio_id", "event_type", "observed_at", "instrument_id",
    "client_order_id", "side", "quantity", "price", "fee", "realized_pnl",
    "result", "payload_json",
)


def _cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    # Do not let imported text become a spreadsheet formula. Negative decimals stay numeric.
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@")) and not re.fullmatch(
        r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text,
    ):
        return "'" + text
    return text


def write_csv(events: Iterable[dict[str, Any]], portfolio_id: str, output: TextIO) -> int:
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    count = 0
    for event in events:
        payload = event["payload"]
        result = payload.get("result")
        fill = result if isinstance(result, dict) and event["event_type"] == "fill" else {}
        row = {
            **{key: event[key] for key in ("sequence", "event_type", "observed_at")},
            "portfolio_id": portfolio_id,
            "instrument_id": (
                fill.get("instrument_id") or payload.get("instrument_id")
                or payload.get("quote", {}).get("instrument_id", "")
            ),
            **{key: fill.get(key, "") for key in (
                "client_order_id", "side", "quantity", "price", "fee", "realized_pnl",
            )},
            "result": "" if result is None or isinstance(result, dict) else result,
            "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        }
        writer.writerow({key: _cell(value) for key, value in row.items()})
        count += 1
    return count


def write_jsonl(events: Iterable[dict[str, Any]], portfolio_id: str, output: TextIO) -> int:
    count = 0
    for event in events:
        output.write(json.dumps({"portfolio_id": portfolio_id, **event}, ensure_ascii=False) + "\n")
        count += 1
    return count


def export_file(
    events: Iterable[dict[str, Any]], portfolio_id: str, destination: str | Path,
    *, format_name: str = "csv",
) -> dict[str, Any]:
    """Publish a complete UTF-8 export without exposing partial output or replacing files."""
    if format_name not in {"csv", "jsonl"}:
        raise TradingError("Export format must be csv or jsonl")
    target = Path(destination)
    if os.path.lexists(target):
        raise TradingError("Export destination already exists; choose a new filename")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".paper-export-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            writer = write_csv if format_name == "csv" else write_jsonl
            count = writer(events, portfolio_id, output)
            output.flush()
            os.fsync(output.fileno())
        size = temporary.stat().st_size
        os.link(temporary, target)
        return {"path": str(target), "format": format_name, "event_count": count, "bytes": size}
    finally:
        temporary.unlink(missing_ok=True)
