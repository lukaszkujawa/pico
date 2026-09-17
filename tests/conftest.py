import sqlite3
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from textual.pilot import Pilot

WAIT_TIMEOUT = 10.0
POLL_INTERVAL = 0.005


def wait_until(
    predicate: Callable[[], bool], description: str, timeout: float = WAIT_TIMEOUT
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_INTERVAL)
    if predicate():
        return
    raise AssertionError(f"timed out after {timeout}s waiting until {description}")


@pytest.fixture(autouse=True)
def close_sqlite_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    yield
    for conn in opened:
        conn.close()


async def settle(pilot: Pilot[None], predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
    raise AssertionError(f"timed out after {WAIT_TIMEOUT}s waiting until {description}")
