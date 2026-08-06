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
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_active = False


def set_backtest_active(active: bool) -> None:
    global _active
    with _lock:
        _active = active


def backtest_active() -> bool:
    with _lock:
        return _active
