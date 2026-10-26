"""Read-only import of completed TradingAgents paper-research artifacts."""

import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prediction_trader.research.context import MAX_CONTEXT_AGE, ResearchContext, utc_timestamp

MAX_RUN_DIRECTORIES = 512
MAX_ARTIFACT_BYTES = 64 * 1024


class ProducerResearchContextProvider:
    """Never supplies a missing evidence timestamp or extends source expiration."""

    def __init__(
        self,
        directory: str = "",
        *,
        expected_symbol: str = "BTC-USD",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._directory = Path(directory) if directory.strip() else None
        self._symbol = expected_symbol
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._scanned_at = float("-inf")
        self._candidate: dict[str, Any] | None = None
        self._scan_status = "missing"
        self._first_seen: dict[str, datetime] = {}

    @staticmethod
    def _read_json(directory_fd: int, name: str) -> dict[str, Any]:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("Invalid artifact")
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError("Oversized artifact")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid artifact")
        return result

    def _scan(self) -> None:
        assert self._directory is not None
        self._candidate = None
        self._scan_status = "missing"
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self._directory, flags)
        except FileNotFoundError:
            return
        except OSError:
            self._scan_status = "invalid"
            return
        candidates: list[tuple[datetime, str, dict[str, Any]]] = []
        try:
            with os.scandir(root_fd) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_RUN_DIRECTORIES:
                        self._scan_status = "scan_limit_exceeded"
                        return
                    if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                        continue
                    run_fd: int | None = None
                    try:
                        run_fd = os.open(entry.name, flags, dir_fd=root_fd)
                        run = self._read_json(run_fd, "run.json")
                        result = self._read_json(run_fd, "result.json")
                        evidence = result.get("evidence")
                        if not isinstance(evidence, dict):
                            continue
                        if run.get("status") != "completed" or (
                            run.get("ticker") != self._symbol
                            or evidence.get("ticker") != self._symbol
                            or run.get("run_id") != result.get("run_id")
                            or result.get("run_id") != entry.name
                        ):
                            continue
                        created_at = utc_timestamp(result["created_at"])
                        if utc_timestamp(run["completed_at"]) != created_at:
                            continue
                        digest = hashlib.sha256(
                            json.dumps(
                                result,
                                sort_keys=True,
                                ensure_ascii=False,
                            ).encode()
                        ).hexdigest()
                        candidates.append((created_at, digest, result))
                    except (OSError, ValueError, TypeError, KeyError, RecursionError):
                        continue
                    finally:
                        if run_fd is not None:
                            os.close(run_fd)
        except OSError:
            self._scan_status = "invalid"
            return
        finally:
            os.close(root_fd)
        if not candidates:
            return
        _, identity, self._candidate = max(candidates, key=lambda item: (item[0], item[1]))
        # Keep this map bounded by the directory limit. Original timestamps are
        # preserved for visible results, and process restart only moves availability later.
        visible = {item[1] for item in candidates}
        self._first_seen = {key: value for key, value in self._first_seen.items() if key in visible}
        observed_at = utc_timestamp(self._clock())
        for _, digest, _ in candidates:
            self._first_seen.setdefault(digest, observed_at)
        self._candidate = dict(self._candidate, _available_at=self._first_seen[identity])
        self._scan_status = "available"

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        if self._directory is None:
            return {"status": "disabled", "context": None}
        checked_at = utc_timestamp(now or self._clock())
        with self._lock:
            monotonic = time.monotonic()
            if monotonic - self._scanned_at >= 2:
                self._scan()
                self._scanned_at = monotonic
            result = self._candidate
            if result is None:
                return {"status": self._scan_status, "context": None}
            evidence = result["evidence"]
            data_as_of = result.get("data_as_of", evidence.get("data_as_of"))
            if data_as_of is None:
                return {"status": "missing_evidence_timestamp", "context": None}
            try:
                # No fallback to an older completed result when this newest one
                # lacks evidence, has expired or has malformed temporal metadata.
                if (
                    "data_as_of" in result
                    and "data_as_of" in evidence
                    and utc_timestamp(result["data_as_of"]) != utc_timestamp(evidence["data_as_of"])
                ):
                    return {"status": "invalid", "context": None}
                if checked_at >= utc_timestamp(result["expires_at"]):
                    return {"status": "stale", "context": None}
                context = ResearchContext.model_validate(
                    {
                        "symbol": self._symbol,
                        "generated_at": result["created_at"],
                        "data_as_of": data_as_of,
                        "available_at": result["_available_at"],
                        "valid_until": result["expires_at"],
                        "rating": result["rating"],
                        "rationale": result["reason"],
                        "source_run_id": result["run_id"],
                    }
                )
            except (ValueError, TypeError, KeyError):
                return {"status": "invalid", "context": None}
            if max(context.generated_at, context.data_as_of, context.available_at) > checked_at:
                return {"status": "future", "context": None}
            if checked_at - context.data_as_of > MAX_CONTEXT_AGE:
                return {"status": "stale", "context": None}
            return {"status": "available", "context": context.model_dump(mode="json")}
