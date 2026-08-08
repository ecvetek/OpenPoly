"""Global guard coordinating a backtest replay with the live pipeline.

Discovered while wiring up the API route: the live ``PipelineOrchestrator``
and ``ExitMonitor`` keep running continuously for the life of the process —
there is no way to pause them. ``main.py``'s lifespan calls
``orch.start()``/``exit_monitor.start()`` once at startup with no matching
stop route anywhere in the API, and the canvas's "Run/Pause" control only
starts/stops the news and market *data sources* — not the orchestrator or
exit monitor themselves (see ``frontend/src/canvas/useRuntime.ts``'s own
docstring: "Pure derivation over existing stores — no new backend
endpoints"). So a guard based on "requires the pipeline to be stopped first"
can never be satisfied; that was the original (wrong) design here.

Worse, a backtest request runs synchronously in FastAPI's thread pool while
the live orchestrator/exit monitor keep running as asyncio tasks on the main
event loop, concurrently. Without this guard, an incoming news item could be
processed by the live entry section — or the exit monitor could evaluate an
open position — *while* ``openpoly.markets.manager.manager.store`` is
swapped to historical data, producing a real trading decision (possibly a
real order) priced off stale/historical data instead of the live market.
For a system that can place real orders with real money, that's a
correctness and safety hazard, not just a display glitch.

The fix: a simple flag, held only for the (typically brief) duration of a
replay, that the live decision paths check and skip on — fail-safe (treated
as "no decision this tick"), not an error, mirroring how those paths already
treat "no order book" / "no position upstream" as an ordinary skip.

``backtest_run()`` below closes a narrower race the flag alone can't: two
concurrent requests can both observe ``backtest_active()`` as False before
either sets it True, then both proceed into ``engine.run_backtest``'s
non-reentrant module-global store swap at the same time. A plain flag is
fine for the live decision paths above (they only ever read it), but the
one path that *claims* the slot needs the check-and-set to be a single
atomic step, not a check then a separate set with a window in between.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_active = False
_run_lock = threading.Lock()


def set_backtest_active(active: bool) -> None:
    global _active
    with _lock:
        _active = active


def backtest_active() -> bool:
    with _lock:
        return _active


class BacktestAlreadyRunning(RuntimeError):
    """Raised by ``backtest_run()`` when another replay already holds the
    slot. A ``RuntimeError`` subclass so existing ``pytest.raises
    (RuntimeError, match="already in progress")`` call sites keep matching
    unchanged."""


@contextmanager
def backtest_run() -> Iterator[None]:
    """Atomically claim the backtest slot for the caller's duration and hold
    ``backtest_active()`` True for it. Never blocks — raises
    ``BacktestAlreadyRunning`` immediately if another replay already holds
    the slot, since callers run in FastAPI's sync thread pool and must not
    tie up a worker thread waiting on another (possibly long) replay."""
    if not _run_lock.acquire(blocking=False):
        raise BacktestAlreadyRunning("a backtest is already in progress")
    try:
        set_backtest_active(True)
        yield
    finally:
        set_backtest_active(False)
        _run_lock.release()
