import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from prediction_trader.services.snapshot_cache import SnapshotCache


class FakeClock:
    def __init__(self, *, monotonic: float = 100, wall: float = 600) -> None:
        self.monotonic = monotonic
        self.wall = wall

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += seconds


def test_concurrent_callers_share_one_load() -> None:
    start = threading.Barrier(9)
    loading = threading.Event()
    release = threading.Event()
    all_readers = threading.Event()
    clock_lock = threading.Lock()
    reader_threads: set[int] = set()
    calls = 0
    snapshot = {"status": "ok"}

    def load() -> dict[str, str]:
        nonlocal calls
        calls += 1
        loading.set()
        assert release.wait(timeout=5)
        return snapshot

    def clock() -> float:
        with clock_lock:
            reader_threads.add(threading.get_ident())
            if len(reader_threads) == 8:
                all_readers.set()
        return 100.0

    cache = SnapshotCache(load, ttl_seconds=5, clock=clock, wall_clock=lambda: 600)

    def read() -> dict[str, str]:
        start.wait(timeout=5)
        return cache.read()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read) for _ in range(8)]
        start.wait(timeout=5)
        try:
            assert loading.wait(timeout=5)
            assert all_readers.wait(timeout=5)
        finally:
            release.set()
        assert all(future.result(timeout=5) is snapshot for future in futures)
    assert calls == 1


def test_expires_at_exact_ttl() -> None:
    clock = FakeClock()
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        return calls

    cache = SnapshotCache(load, clock=lambda: clock.monotonic, wall_clock=lambda: clock.wall)
    assert cache.read() == 1
    clock.advance(0.25)
    assert cache.read() == 1
    clock.advance(0.25)
    assert cache.read() == 2


def test_expired_load_failure_never_returns_stale_and_next_call_retries() -> None:
    clock = FakeClock()
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database temporarily unavailable")
        return calls

    cache = SnapshotCache(load, clock=lambda: clock.monotonic, wall_clock=lambda: clock.wall)
    assert cache.read() == 1
    clock.advance(0.5)
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        cache.read()
    assert cache.read() == 3
    assert cache.read() == 3


@pytest.mark.parametrize("window_seconds", [300, 900])
def test_expiry_is_capped_at_configured_window_boundary(window_seconds: int) -> None:
    clock = FakeClock(wall=3 * window_seconds - 0.25)
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        return calls

    cache = SnapshotCache(
        load,
        window_seconds=window_seconds,
        clock=lambda: clock.monotonic,
        wall_clock=lambda: clock.wall,
    )
    assert cache.read() == 1
    clock.advance(0.125)
    assert cache.read() == 1
    clock.advance(0.125)
    assert cache.read() == 2


@pytest.mark.parametrize("window_seconds", [300, 900])
def test_load_crossing_window_boundary_is_reloaded_before_returning(window_seconds: int) -> None:
    clock = FakeClock(wall=3 * window_seconds - 0.25)
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.advance(0.5)
        return calls

    cache = SnapshotCache(
        load,
        window_seconds=window_seconds,
        clock=lambda: clock.monotonic,
        wall_clock=lambda: clock.wall,
    )
    assert cache.read() == 2
    assert cache.read() == 2


def test_fifteen_minute_window_does_not_expire_at_five_minute_boundary() -> None:
    clock = FakeClock(wall=599.75)
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        return calls

    cache = SnapshotCache(
        load,
        ttl_seconds=2,
        window_seconds=900,
        clock=lambda: clock.monotonic,
        wall_clock=lambda: clock.wall,
    )
    assert cache.read() == 1
    clock.advance(0.25)
    assert cache.read() == 1


def test_slow_load_does_not_extend_ttl() -> None:
    clock = FakeClock()
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        clock.advance(0.75)
        return calls

    cache = SnapshotCache(load, clock=lambda: clock.monotonic, wall_clock=lambda: clock.wall)
    assert cache.read() == 1
    assert cache.read() == 2


@pytest.mark.parametrize("jump", [-300, 300])
def test_wall_clock_window_change_invalidates_snapshot(jump: float) -> None:
    clock = FakeClock()
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        return calls

    cache = SnapshotCache(load, clock=lambda: clock.monotonic, wall_clock=lambda: clock.wall)
    assert cache.read() == 1
    clock.wall += jump
    assert cache.read() == 2


def test_none_is_a_cacheable_snapshot() -> None:
    calls = 0

    def load() -> None:
        nonlocal calls
        calls += 1

    cache = SnapshotCache(load, clock=lambda: 100, wall_clock=lambda: 600)
    assert cache.read() is None
    assert cache.read() is None
    assert calls == 1


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        SnapshotCache(lambda: {}, ttl_seconds=ttl)


@pytest.mark.parametrize("window_seconds", [0, -1, 300.0, True, "900"])
def test_rejects_invalid_window(window_seconds: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SnapshotCache(lambda: {}, window_seconds=window_seconds)
