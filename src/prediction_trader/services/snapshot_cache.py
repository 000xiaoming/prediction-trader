import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass


@dataclass(frozen=True)
class _Snapshot[T]:
    value: T
    expires_at: float
    window: int


class SnapshotCache[T]:
    """Share one snapshot load across callers, with no stale-on-error fallback.

    TTL starts before loading so a slow query cannot extend snapshot freshness.
    The wall clock additionally invalidates cached snapshots at UTC market-window
    boundaries; the monotonic clock protects TTL against wall-clock adjustments.
    Loaders must be read-only and callers must not mutate the returned snapshot.
    """

    def __init__(
        self,
        loader: Callable[[], T],
        *,
        ttl_seconds: float = 0.5,
        window_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        if type(window_seconds) is not int or window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")
        self._loader = loader
        self._ttl_seconds = ttl_seconds
        self._window_seconds = window_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._snapshot: _Snapshot[T] | None = None
        self._pending: Future[T] | None = None

    def read(self) -> T:
        with self._lock:
            now = self._clock()
            window = math.floor(self._wall_clock() / self._window_seconds)
            if (
                self._snapshot is not None
                and now < self._snapshot.expires_at
                and window == self._snapshot.window
            ):
                return self._snapshot.value
            self._snapshot = None
            leader = self._pending is None
            if self._pending is None:
                self._pending = Future()
            pending = self._pending

        if not leader:
            return pending.result()

        try:
            while True:
                started_at = self._clock()
                started_wall = self._wall_clock()
                window = math.floor(started_wall / self._window_seconds)
                value = self._loader()
                with self._lock:
                    # A query begun in the old market window must be reloaded.
                    if math.floor(self._wall_clock() / self._window_seconds) != window:
                        continue
                    remaining = self._window_seconds - started_wall % self._window_seconds
                    self._snapshot = _Snapshot(
                        value=value,
                        expires_at=started_at + min(self._ttl_seconds, remaining),
                        window=window,
                    )
                    self._pending = None
                    pending.set_result(value)
                    return value
        except BaseException as error:
            with self._lock:
                self._pending = None
                pending.set_exception(error)
            raise
