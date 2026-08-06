"""Exit monitor — the position-driven, timer-driven close loop.

The news pipeline (orchestrator) is event-driven; closing a position is
position-driven + periodic. ``ExitMonitor`` runs a tick loop: every
``tick_interval_seconds`` it walks every open position, marks it with the held
side's current price (level-1 bid of the held token's order book), runs the
``exit`` section, and — when the section returns a ``CloseIntent`` — routes it
to ``executor.execute_sell``. Each evaluation is recorded in ``exit_log``.

It shares the one module-level ``executor`` with the orchestrator — entry buys
and exit sells go through the same fill path. The ``PortfolioStore`` is
injected by the FastAPI lifespan once the DB is up.

A position whose market has resolved drops out of the catalog → no order book →
the monitor logs a ``skip`` and leaves the position open. Settlement-close is a
separate concern, out of scope.

The tick does only sync work (DB read/write, in-memory book lookup, the pure
exit section) — all sub-millisecond — so it runs inline; the loop yields
cooperatively between ticks (docs/architecture/05).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.backtest.guard import backtest_active
from openpoly.db.tables import OrderBookSnapshot
from openpoly.execution import ExecResult
from openpoly.execution import executor as _executor_singleton
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.store import MarketStore
from openpoly.portfolio import HeldPosition, PortfolioStore
from openpoly.runtime.section_log import ExitDecision, exit_log
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.exit.threshold_v0 import (
    CloseIntent,
    MarkedPosition,
    ThresholdExitConfig,
    ThresholdExitV0,
)

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 30  # matches ThresholdExitConfig's own default
TICK_EVENT_RING_MAXLEN = 200  # mirrors markets.manager.EVENT_RING_MAXLEN
# Mirrors portfolio.store._QTY_EPS — same "close vs. reduce" epsilon
# record_sell uses, applied here to decide whether a fill actually closed
# the position (drop tracked state) or just reduced it (keep tracking).
_QTY_EPS = 1e-6

State = Literal["stopped", "running"]

TickEventKind = Literal["started", "stopped", "tick_ok", "tick_error"]


@dataclass(frozen=True)
class TickEvent:
    """One sweep-level lifecycle/heartbeat event — the "does this node
    actually execute" timeline, parallel to MarketSourceManager's LogEvent
    ring. One entry per tick (not per position), so it stays bounded
    regardless of how many positions are open."""

    ts: float
    kind: TickEventKind
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class TickSummary:
    """The last sweep's outcome breakdown — parallel to markets.store's
    PollSummary. ``reason_counts`` keys are ``no_order_book``,
    ``within_thresholds``, ``stop_loss``, ``peak_drawdown``, ``take_profit``,
    ``error`` — whichever occurred this tick."""

    ts: float
    evaluated: int
    closed: int
    reason_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "evaluated": self.evaluated,
            "closed": self.closed,
            "reason_counts": dict(self.reason_counts),
        }


_CLOSE_REASONS = (
    "stop_loss",
    "peak_drawdown",
    "take_profit",
    "scale_out",
    "post_scale_out_stop",
    "final_take_profit",
)


class _ExitSection(Protocol):
    """Minimal exit-section shape used by the monitor."""

    def run(self, input: SectionInput) -> SectionOutput: ...


class _Executor(Protocol):
    """Minimal executor shape used by the monitor."""

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: str,
        ts: float,
        trigger: str | None,
        qty: float | None = None,
    ) -> ExecResult: ...


class ExitMonitor:
    """Timer-driven loop that closes open positions via the exit section."""

    def __init__(
        self,
        *,
        exit_section: _ExitSection,
        executor: _Executor,
        tick_interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
    ) -> None:
        self._exit = exit_section
        self._executor = executor
        # Initial value only — ThresholdExitConfig.tick_interval_seconds is
        # now the source of truth once a real config is loaded; see
        # replace_exit_section below, which keeps this in sync on every
        # canvas-driven swap (startup and live hot-reload alike).
        self._tick_interval = tick_interval_seconds
        self._portfolio: PortfolioStore | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state: State = "stopped"
        # canvas-sync v2: atomic swap lock — same model as orchestrator's
        # _sections_lock. _tick_once reads self._exit; replace happens between
        # ticks (or between in-flight section.run calls within a tick — Python
        # GC keeps the old instance alive for any caller already holding it).
        self._exit_lock = asyncio.Lock()
        # Per-position peak of the held side's current_price across this
        # process's lifetime. Rebuilt at startup by ``bootstrap_peaks`` from
        # the order_book_snapshot table; updated every tick; dropped on close.
        # Process-restart loses anything not in that table — accepted trade-off
        # for keeping runtime state out of the database schema.
        self._peak: dict[int, float] = {}
        # Per-position "has this position already taken its scale-out partial
        # sell" flag — same lifecycle as ``self._peak`` (rebuilt at startup by
        # ``bootstrap_scaled_out``, updated on a scale-out fill, dropped when
        # the position fully closes, pruned if it closes via another path).
        # Only ``exit.scale_out_v0.ScaleOutExitV0`` reads this (injected into
        # MarkedPosition.scaled_out); the baseline ThresholdExitV0 ignores it.
        self._scaled_out: dict[int, bool] = {}
        # Tick telemetry (v18) — the "is the monitor working" heartbeat,
        # surfaced via /api/exit/log so the canvas badge / Closes tab can show
        # liveness without flooding exit_log with a skip entry per position per
        # tick. Within-threshold + no-order-book evaluations no longer write a
        # log entry at all (the ring keeps only the rare ok / error closes, so
        # they never get evicted); these counts carry that signal instead.
        self._last_tick_at: float | None = None
        self._last_tick_open: int = 0
        self._last_tick_blocked: int = 0
        # Per-tick evaluated/closed/reason_counts breakdown + a bounded
        # started/stopped/tick_ok/tick_error event ring — the "last poll"
        # histogram and "events" timeline equivalents from market_source's
        # Live tab, surfaced via /api/exit/log so the Closes tab can show
        # skips and closes without logging one entry per position per tick.
        self._last_tick: TickSummary | None = None
        self._tick_events: deque[TickEvent] = deque(maxlen=TICK_EVENT_RING_MAXLEN)
        # Persist hook — optional, set by main.py's lifespan once the DB is
        # up. None means "not wired yet"; append still happens to exit_log
        # (the in-memory ring) regardless.
        self._exit_persist: Callable[[ExitDecision], None] | None = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def last_tick_at(self) -> float | None:
        """Wall-clock of the last completed sweep (None before the first)."""
        return self._last_tick_at

    @property
    def open_positions(self) -> int:
        """Open positions seen on the last sweep."""
        return self._last_tick_open

    @property
    def blocked(self) -> int:
        """Positions on the last sweep that could not be evaluated (no order
        book — market resolved or data gap; their stop-loss can't fire)."""
        return self._last_tick_blocked

    @property
    def last_tick(self) -> dict[str, Any] | None:
        """The last sweep's evaluated/closed/reason_counts breakdown, or
        None before the first tick."""
        return self._last_tick.to_dict() if self._last_tick is not None else None

    def tick_events(self, limit: int = TICK_EVENT_RING_MAXLEN) -> list[dict[str, Any]]:
        """Oldest-first tick events, capped at ``limit`` — same slicing
        contract as SectionLogStore.entries()."""
        events = list(self._tick_events)
        if limit is not None and 0 <= limit < len(events):
            events = events[-limit:]
        return [e.to_dict() for e in events]

    def _record_tick_event(self, kind: TickEventKind, detail: str | None = None) -> None:
        self._tick_events.append(TickEvent(ts=time.time(), kind=kind, detail=detail))

    def configure(self, portfolio: PortfolioStore) -> None:
        """Inject the PortfolioStore — the FastAPI lifespan calls this once the
        DB is up. Construction itself touches no DB."""
        self._portfolio = portfolio

    def set_exit_persist(self, hook: Callable[[ExitDecision], None] | None) -> None:
        self._exit_persist = hook

    def bootstrap_peaks(self, session_factory: sessionmaker[Session]) -> None:
        """Rebuild per-position peaks from persisted order-book snapshots.

        For each open position, scan ``order_book_snapshot`` rows where
        ``token_id == position.token_id AND recorded_at >= opened_at`` and
        take the max of ``bids[0][0]`` (the held-side best bid — same value
        the live tick uses). Falls back to ``avg_entry_price`` when no
        snapshot exists yet. Called once at startup, before ``start()``.
        """
        if self._portfolio is None:
            return
        opens = self._portfolio.get_open_positions()
        if not opens:
            return
        with session_factory() as session:
            for held in opens:
                stmt = select(OrderBookSnapshot.bids_json).where(
                    OrderBookSnapshot.token_id == held.token_id,
                    OrderBookSnapshot.recorded_at >= held.opened_at,
                )
                peak = held.avg_entry_price
                for (bids_json,) in session.execute(stmt):
                    try:
                        bids = json.loads(bids_json)
                    except (TypeError, ValueError):
                        continue
                    if bids:
                        bid = float(bids[0][0])
                        if bid > peak:
                            peak = bid
                self._peak[held.position_id] = peak
        logger.info("exit monitor: bootstrap_peaks loaded %d positions", len(self._peak))

    def bootstrap_scaled_out(self) -> None:
        """Rebuild per-position ``scaled_out`` flags from the fill ledger.

        Unlike ``bootstrap_peaks`` (which reads ``order_book_snapshot``
        directly and so needs a raw session factory), this goes through
        ``PortfolioStore.has_scale_out_fill`` — a prior sell fill with
        ``trigger == "scale_out"`` on that position_id — so it only needs
        the already-injected ``self._portfolio``.

        Without this, a restart mid-position would forget the partial sell
        already happened, re-fire the pre-scale-out branch, and sell
        ``scale_out_fraction`` of the *already-reduced* qty — a real drift
        bug, not just redundant work. Called once at startup, before
        ``start()``, same as ``bootstrap_peaks``.
        """
        if self._portfolio is None:
            return
        opens = self._portfolio.get_open_positions()
        if not opens:
            return
        for held in opens:
            if self._portfolio.has_scale_out_fill(held.position_id):
                self._scaled_out[held.position_id] = True
        logger.info("exit monitor: bootstrap_scaled_out loaded %d positions", len(self._scaled_out))

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._state = "running"
        # Recreate the Event each start so it binds to the *current* loop —
        # this module singleton may be start()ed across distinct loops (tests).
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._tick_loop())
        self._record_tick_event("started")

    async def stop(self) -> None:
        """Signal the tick loop to stop, then await its natural exit — never
        ``task.cancel()`` here. The tick body runs via
        ``asyncio.to_thread(self._tick_once)``, and cancelling the awaiting
        coroutine does **not** stop that worker thread: it keeps running
        through ``_evaluate`` → ``execute_sell`` (a blocking CLOB round-trip in
        live mode) → ``_log`` → ``_exit_persist``. So a cancel returns while a
        real sell is still in flight, and the lifespan then tears down the DB
        writer out from under it — an on-chain fill with no ledger record.

        The loop checks ``_stop`` only between ticks and its interval wait wakes
        early on it, so this waits for exactly one in-flight tick. Mirrors
        ``PipelineOrchestrator.stop`` and ``WriteBehindWriter.stop``."""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        self._state = "stopped"
        self._record_tick_event("stopped")

    # ---------- loop ----------

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._tick_once)
            except Exception:  # noqa: BLE001 — the loop must survive any tick error
                logger.exception("exit monitor: tick failed")
            # Cooperative yield, then sleep the interval — waking early on stop.
            await asyncio.sleep(0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_interval)

    # ---------- tick ----------

    def _tick_once(self) -> None:
        """One sweep — evaluate every open position. Sync; tests drive it
        directly, and ``_tick_loop`` runs it via ``asyncio.to_thread`` so a
        blocking live-executor sell doesn't stall the event loop for the
        whole tick. Records tick telemetry (open / blocked counts +
        timestamp) plus a ``last_tick`` reason-count breakdown and a
        ``tick_ok``/``tick_error`` event — within-threshold and no-order-book
        holds no longer write a log entry to ``exit_log`` (only ok / error
        closes do), but they DO count towards ``last_tick.reason_counts``."""
        if self._portfolio is None:
            return
        # A backtest replay swaps the live MarketStore global for historical
        # data for its duration — see openpoly.backtest.guard's module
        # docstring for why this check exists (there is no way to pause this
        # monitor via the API, so the live exit sweep must skip itself for
        # the swap window instead, rather than risk running the real exit
        # section — and a real sell — against frozen historical data).
        if backtest_active():
            self._record_tick_event("tick_ok", detail="skipped: backtest in progress")
            return
        ts = time.time()
        try:
            catalog = market_source_manager.store
            opens = self._portfolio.get_open_positions()
            # A position can close via a path other than this monitor's own
            # _evaluate() below (SettlementMonitor, manual close,
            # reconciliation) — self-heal any now-stale peak entry here rather
            # than coupling to those other monitors, which deliberately stay
            # independent of this one (see module docstrings). Otherwise
            # _peak grows unboundedly for the life of the process, since
            # settlement is the normal way most positions eventually close.
            open_ids = {held.position_id for held in opens}
            for stale_id in set(self._peak) - open_ids:
                del self._peak[stale_id]
            for stale_id in set(self._scaled_out) - open_ids:
                del self._scaled_out[stale_id]
            reason_counts: dict[str, int] = {}
            for held in opens:
                try:
                    outcome = self._evaluate(held, catalog, ts)
                except Exception as exc:  # noqa: BLE001 — one bad position must not abort the sweep
                    logger.exception("exit monitor: position %d failed", held.position_id)
                    self._log(held, ts, verdict="error", error=repr(exc)[:200])
                    outcome = "error"
                reason_counts[outcome] = reason_counts.get(outcome, 0) + 1
        except Exception as exc:  # noqa: BLE001 — surfaced as a tick_error event, then re-raised so _tick_loop still logs it
            self._record_tick_event("tick_error", detail=str(exc)[:200])
            raise

        closed = sum(reason_counts.get(k, 0) for k in _CLOSE_REASONS)
        blocked = reason_counts.get("no_order_book", 0)
        self._last_tick_at = ts
        self._last_tick_open = len(opens)
        self._last_tick_blocked = blocked
        self._last_tick = TickSummary(
            ts=ts, evaluated=len(opens), closed=closed, reason_counts=reason_counts
        )
        detail = f"{len(opens)} evaluated → {closed} closed"
        if blocked:
            detail += f", {blocked} blocked"
        self._record_tick_event("tick_ok", detail=detail)

    def _evaluate(self, held: HeldPosition, catalog: MarketStore, ts: float) -> str:
        """Evaluate one position. Returns the outcome reason key tallied into
        ``last_tick.reason_counts``: ``no_order_book`` (couldn't evaluate),
        ``within_thresholds`` (held, no trigger), a close trigger
        (``stop_loss``/``peak_drawdown``/``take_profit``), or ``error`` (close
        attempted but the sell didn't fill). Only ok / error closes are
        logged to ``exit_log`` — within-threshold and no-order-book holds are
        not (see tick telemetry)."""
        book = catalog.get_order_book(held.token_id)
        if book is None or not book.bids:
            return "no_order_book"
        current_price = book.bids[0][0]
        # Monotone-increasing per-position peak. New open positions seed at
        # current_price; bootstrap_peaks may have seeded a higher one already.
        prev_peak = self._peak.get(held.position_id, current_price)
        peak_price = max(prev_peak, current_price)
        self._peak[held.position_id] = peak_price

        marked = MarkedPosition(
            market_id=held.market_id,
            side=held.side,
            avg_entry_price=held.avg_entry_price,
            qty=held.qty,
            current_price=current_price,
            peak_price=peak_price,
            scaled_out=self._scaled_out.get(held.position_id, False),
        )
        out = self._exit.run(SectionInput(tick_type="hard", payload=marked))
        return_pct = out.signals.get("return_pct")
        if out.verdict != "ok" or not isinstance(out.payload, CloseIntent):
            # Held within thresholds — no close, no log entry (peak already
            # tracked above; tick telemetry records that this position was
            # evaluated).
            return "within_thresholds"

        intent = out.payload
        result = self._executor.execute_sell(
            held, close_reason=intent.trigger, ts=ts, trigger=intent.trigger, qty=intent.qty
        )
        if result.filled and result.price is not None:
            # Mark against the qty that actually filled, not the qty we asked
            # for: a live GTC sell can partially fill, and record_sell keeps
            # the position open with the remainder. Using held.qty here
            # overstated the log entry (and the PositionDetail UI) on every
            # partial close, while the DB's own realized_pnl was correct.
            filled_qty = result.qty if result.qty is not None else held.qty
            realized = (result.price - held.avg_entry_price) * filled_qty
            # A scale-out (or a liquidity-thin full-close attempt) can fill
            # less than held.qty and leave the position open with the
            # remainder — record_sell's own close-or-reduce decision (same
            # epsilon it uses). Only drop this position's tracked state once
            # it's ACTUALLY gone; popping peak/scaled_out on a still-open
            # remainder would lose exactly the state the post-scale-out phase
            # needs, and re-arm a scale-out that already fired.
            position_closed = filled_qty >= held.qty - _QTY_EPS
            if position_closed:
                self._peak.pop(held.position_id, None)
                self._scaled_out.pop(held.position_id, None)
            elif intent.trigger == "scale_out":
                self._scaled_out[held.position_id] = True
            self._log(
                held,
                ts,
                verdict="ok",
                trigger=intent.trigger,
                return_pct=return_pct,
                peak_price=peak_price,
                fill_price=result.price,
                realized_pnl=realized,
                reason=intent.trigger,
            )
            return intent.trigger
        # The section decided to close but the fill did not land — a
        # position that should be closed is still open: surface as error.
        self._log(
            held,
            ts,
            verdict="error",
            trigger=intent.trigger,
            return_pct=return_pct,
            peak_price=peak_price,
            error=f"sell not filled: {result.skip_reason}",
        )
        return "error"

    def _log(
        self,
        held: HeldPosition,
        ts: float,
        *,
        verdict: str,
        trigger: str | None = None,
        return_pct: float | None = None,
        peak_price: float | None = None,
        fill_price: float | None = None,
        realized_pnl: float | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        entry = ExitDecision(
            ts=ts,
            position_id=held.position_id,
            market_id=held.market_id,
            side=held.side,
            verdict=verdict,  # type: ignore[arg-type]
            trigger=trigger,
            return_pct=return_pct,
            peak_price=peak_price,
            fill_price=fill_price,
            realized_pnl=realized_pnl,
            reason=reason,
            error=error,
        )
        exit_log.append(entry)
        if self._exit_persist is not None:
            try:
                self._exit_persist(entry)
            except Exception:  # noqa: BLE001 — a bad persist hook must not break the monitor
                logger.exception("exit_persist raised; suppressing")

    # ---------- canvas-sync v2: hot-swap the exit section ----------

    async def replace_exit_section(self, new_section: _ExitSection) -> None:
        """Swap the exit section without restarting the monitor.

        Callers (``api/canvas_routes._apply_canvas_reload``, and
        ``api/main.py``'s lifespan at startup) build the new instance from the
        latest canvas, then await this. Same atomicity story as the
        orchestrator: an in-flight ``self._exit.run(...)`` keeps a reference to
        the old instance via Python GC; the next tick reads ``self._exit`` and
        gets the new one."""
        async with self._exit_lock:
            self._exit = new_section
            # tick_interval_seconds lives on the same config as the thresholds,
            # so a canvas save that changes it must take effect the same way —
            # on the loop's next wait, not just on the next process restart.
            # Defensive getattr: _ExitSection is deliberately a minimal Protocol
            # (just .run()) so other exit-section implementations can plug in
            # without necessarily exposing this field — if absent, the monitor
            # simply keeps ticking at whatever cadence it already had.
            new_interval = getattr(
                getattr(new_section, "config", None), "tick_interval_seconds", None
            )
            if isinstance(new_interval, int):
                self._tick_interval = new_interval


# Module-level singleton — the FastAPI lifespan injects its PortfolioStore via
# configure() and start()s it. Shares the one executor with the orchestrator.
exit_monitor = ExitMonitor(
    exit_section=ThresholdExitV0(ThresholdExitConfig()),
    executor=_executor_singleton,
)
