"""Portable paper-event exports; preserve decimal strings and the complete original payload."""

import csv
import json
import re
from collections.abc import Iterable
from typing import Any, TextIO

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


def write_csv(events: Iterable[dict[str, Any]], portfolio_id: str, output: TextIO) -> None:
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
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


def write_jsonl(events: Iterable[dict[str, Any]], portfolio_id: str, output: TextIO) -> None:
    for event in events:
        output.write(json.dumps({"portfolio_id": portfolio_id, **event}, ensure_ascii=False) + "\n")
