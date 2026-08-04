"""Settlement monitor — closes resolved-market positions at 0/1 final price.

Slice E. When a Polymarket market resolves, ``outcomePrices`` is stamped on
the Gamma market record. The exit-monitor doesn't catch this because the
resolved market typically drops out of the discovery catalog (Gamma's
``/events`` is filtered to ``closed=false``), so any open position on it
gets stuck at ``status=open``.

This monitor sweeps periodically (default every 5 min — settlement is slow,
no need to poll fast), groups open positions by ``condition_id``, fetches
those specific markets via the dedicated ``fetch_markets_by_condition_id``
path (which does NOT pass ``closed=false``), and for any resolved one calls
``PortfolioStore.close_position(reason="settlement", sell_price=0_or_1)``
directly — no broker tx, no CLOB call, no on-chain redemption.

CTF redemption (winning tokens → pUSD on the DepositWallet) is a separate
on-chain action that is **out of slice E scope** — slice C V2 pivot
corrigendum §C.SliceE.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Awaitable, Literal, Protocol

from openpoly.markets.models import normalize_gamma_market
from openpoly.markets.polymarket_api import fetch_markets_by_condition_id
from openpoly.portfolio import HeldPosition, PortfolioStore
from openpoly.runtime.section_log import SettlementDecision, settlement_log

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 300  # 5 min — settlement isn't latency-sensitive
TICK_EVENT_RING_MAXLEN = 200  # mirrors exit_monitor.TICK_EVENT_RING_MAXLEN

State = Literal["stopped", "running"]

TickEventKind = Literal["started", "stopped", "tick_ok", "tick_error"]

# Outcomes that are counted into ``last_tick.reason_counts`` rather than
# written to ``settlement_log``: ``still_trading``, ``no_outcome_prices``,
# ``ambiguous_outcome``, ``market_not_returned_by_gamma``,
# ``gamma_fetch_failed``, ``market_normalize_failed``. Each is *steady state*
# for an open position — an unresolved market reports the same one on every
# tick, forever — so a log entry per position per tick flooded the 200-entry
# ring (evicting the ok/error entries that matter, plus the reconciliation
# monitor's alerts, which share it) and grew settlement_decision by ~288
# rows/position/day with no signal. Only real settlements and genuine
# per-position failures still reach the log. Mirrors the same call the exit
# monitor made for its within-threshold holds.


@dataclass(frozen=True)
class TickEvent:
    """One sweep-level lifecycle/heartbeat event — parallel to ExitMonitor's
    ring. One entry per tick, not per position, so it stays bounded however
    many positions are open."""

    ts: float
    kind: TickEventKind
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class TickSummary:
    """The last sweep's outcome breakdown. ``reason_counts`` keys are whichever
    of ``settled`` / ``still_trading`` / ``no_outcome_prices`` /
    ``ambiguous_outcome`` / ``market_not_returned_by_gamma`` /
    ``gamma_fetch_failed`` / ``market_normalize_failed`` /
    ``already_closed_by_other_monitor`` / ``close_failed`` occurred."""

    ts: float
    evaluated: int
    settled: int
    reason_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "evaluated": self.evaluated,
            "settled": self.settled,
            "reason_counts": dict(self.reason_counts),
        }


class _MarketFetcher(Protocol):
    """Async callable: (condition_ids: list[str]) -> list[raw_market_dict]."""

    def __call__(self, condition_ids: list[str]) -> Awaitable[list[dict]]: ...


def _settlement_price_for_side(outcome_prices: tuple[float, float], side: str) -> float | None:
    """Map Gamma's resolved ``outcomePrices`` to a 0/1 final price for the
    held side.

    Polymarket exposes outcomes as YES=index 0 / NO=index 1, with prices
    summing to 1 once resolved (``[1, 0]`` YES wins, ``[0, 1]`` NO wins).
    Returns None when the resolution is ambiguous (e.g. ``[0.5, 0.5]``
    after a disputed market goes to split) — caller skips and waits.
    """
    yes_price, no_price = outcome_prices
    # Only accept clean 0/1 outcomes; anything else (split / unresolved)
    # means downstream PnL math would be unreliable.
    if {round(yes_price, 4), round(no_price, 4)} != {0.0, 1.0}:
        return None
    if side == "yes":
        return float(yes_price)
    if side == "no":
        return float(no_price)
    return None


class SettlementMonitor:
    """Timer-driven loop that closes resolved-market positions at 0/1."""

    def __init__(
        self,
        *,
        fetcher: _MarketFetcher = fetch_markets_by_condition_id,
        tick_interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
    ) -> None:
        self._fetcher = fetcher
        self._tick_interval = tick_interval_seconds
        self._portfolio: PortfolioStore | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state: State = "stopped"
        # Persist hook — optional, set by main.py's lifespan once the DB is
        # up. None means "not wired yet"; append still happens to
        # settlement_log (the in-memory ring) regardless.
        self._settlement_persist: Callable[[SettlementDecision], None] | None = None
        # Tick telemetry — the "is the monitor working" heartbeat that replaces
        # the per-position-per-tick skip entries (see the module-level note above).
        self._last_tick_at: float | None = None
        self._last_tick_open: int = 0
        self._last_tick: TickSummary | None = None
        self._tick_events: deque[TickEvent] = deque(maxlen=TICK_EVENT_RING_MAXLEN)

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
    def last_tick(self) -> dict[str, Any] | None:
        """The last sweep's evaluated/settled/reason_counts breakdown."""
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
        """Inject PortfolioStore — FastAPI lifespan calls once DB is up."""
        self._portfolio = portfolio

    def set_settlement_persist(self, hook: Callable[[SettlementDecision], None] | None) -> None:
        self._settlement_persist = hook

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._state = "running"
        # Recreate the Event each start so it binds to the current loop —
        # this module singleton may be start()ed across distinct loops in tests.
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._tick_loop())
        self._record_tick_event("started")

    async def stop(self) -> None:
        """Signal the tick loop to stop, then await its natural exit — never
        ``task.cancel()``. A cancel mid-tick can abandon a ``close_position``
        between its Gamma read and its DB write, and returns before the
        settlement log entry is persisted. Same discipline as
        ``PipelineOrchestrator.stop``; the loop checks ``_stop`` between ticks
        and its interval wait wakes early on it."""
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
                await self._tick_once()
            except Exception:  # noqa: BLE001 — loop must survive any tick error
                logger.exception("settlement monitor: tick failed")
            await asyncio.sleep(0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_interval)

    # ---------- tick ----------

    async def _tick_once(self) -> None:
        """One sweep over every open position.

        Steady-state outcomes (an unresolved market, a Gamma outage) are
        tallied into ``last_tick.reason_counts`` rather than written as a log
        entry per position per tick — see the note near the top of this module.
        Only actual settlements and genuine per-position failures reach
        ``settlement_log``.
        """
        if self._portfolio is None:
            return
        opens = self._portfolio.get_open_positions()
        self._last_tick_open = len(opens)
        counts: dict[str, int] = {}
        if not opens:
            self._finish_tick(evaluated=0, counts=counts)
            return

        # Group positions by condition_id so the same Gamma row resolves all
        # positions on that market in one pass (e.g. YES + NO opened in
        # different cycles still share one condition_id).
        by_cid: dict[str, list[HeldPosition]] = {}
        for held in opens:
            by_cid.setdefault(held.condition_id, []).append(held)

        try:
            raw_markets = await self._fetcher(list(by_cid))
        except Exception as exc:  # noqa: BLE001 — network errors must not crash loop
            logger.warning("settlement monitor: Gamma fetch failed: %s", exc)
            # One tick-level event carries the outage; the per-position count
            # says how much is going unchecked. Previously this wrote one row
            # per position per tick for the whole duration of the outage.
            counts["gamma_fetch_failed"] = len(opens)
            self._finish_tick(
                evaluated=len(opens),
                counts=counts,
                error=f"gamma_fetch_failed: {type(exc).__name__}",
            )
            return

        # Index returned markets by condition_id; some requested may be absent
        # (Gamma occasionally returns a partial list) — those just get counted.
        returned_cids: set[str] = set()
        for raw in raw_markets:
            cid = raw.get("conditionId") or raw.get("condition_id")
            if not cid:
                continue
            returned_cids.add(str(cid))
            self._process_market(raw, by_cid.get(str(cid), []), counts)

        for cid, held_list in by_cid.items():
            if cid not in returned_cids:
                for _held in held_list:
                    self._count(counts, "market_not_returned_by_gamma")

        self._finish_tick(evaluated=len(opens), counts=counts)

    @staticmethod
    def _count(counts: dict[str, int], key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    def _finish_tick(
        self, *, evaluated: int, counts: dict[str, int], error: str | None = None
    ) -> None:
        ts = time.time()
        settled = counts.get("settled", 0)
        self._last_tick_at = ts
        self._last_tick = TickSummary(
            ts=ts, evaluated=evaluated, settled=settled, reason_counts=counts
        )
        if error is not None:
            self._record_tick_event("tick_error", detail=error)
            return
        detail = f"{evaluated} evaluated → {settled} settled"
        self._record_tick_event("tick_ok", detail=detail)

    def _process_market(
        self, raw: dict, held_positions: list[HeldPosition], counts: dict[str, int]
    ) -> None:
        ts = time.time()
        # Reuse the existing parser for consistency; outcome_prices is the
        # new (slice E) field we rely on here.
        market = normalize_gamma_market(raw, event=None)
        if market is None:
            for _held in held_positions:
                self._count(counts, "market_normalize_failed")
            return

        if not market.closed:
            for _held in held_positions:
                self._count(counts, "still_trading")
            return

        if market.outcome_prices is None:
            for _held in held_positions:
                self._count(counts, "no_outcome_prices")
            return

        for held in held_positions:
            final_price = _settlement_price_for_side(market.outcome_prices, held.side)
            if final_price is None:
                self._count(counts, "ambiguous_outcome")
                continue
            try:
                self._portfolio.close_position(  # type: ignore[union-attr]
                    held.position_id,
                    sell_price=final_price,
                    ts=ts,
                    close_reason="settlement",
                    trigger="settlement",
                    order_id=None,
                    tx_hash=None,
                )
            except ValueError as exc:
                # close_position raises ValueError when the position is no
                # longer open — most commonly because ExitMonitor (or a
                # manual close) beat this monitor to it between the
                # open-positions snapshot at the top of _tick_once and this
                # call. That's a benign race, not a real failure; only log
                # it as an error if the position turns out to genuinely not
                # be closed (a real bug, not a race) — never silently mask
                # an actual failure as benign.
                record = self._portfolio.get_position(held.position_id)  # type: ignore[union-attr]
                if record is not None and record.status == "closed":
                    # Kept as a real log entry, not a counter: it fires at most
                    # once per position (the position is closed afterwards), so
                    # it can't flood, and it explains why a settlement the
                    # operator expected didn't come from this monitor.
                    self._count(counts, "already_closed_by_other_monitor")
                    self._log(
                        held,
                        ts,
                        verdict="skip",
                        final_price=final_price,
                        reason="already_closed_by_other_monitor",
                    )
                else:
                    logger.exception(
                        "settlement monitor: close_position failed for %d",
                        held.position_id,
                    )
                    self._count(counts, "close_failed")
                    self._log(
                        held,
                        ts,
                        verdict="error",
                        final_price=final_price,
                        error=f"close_failed: {type(exc).__name__}: {str(exc)[:160]}",
                    )
                continue
            except Exception as exc:  # noqa: BLE001 — one bad close must not abort
                logger.exception(
                    "settlement monitor: close_position failed for %d",
                    held.position_id,
                )
                self._count(counts, "close_failed")
                self._log(
                    held,
                    ts,
                    verdict="error",
                    final_price=final_price,
                    error=f"close_failed: {type(exc).__name__}: {str(exc)[:160]}",
                )
                continue
            realized = (final_price - held.avg_entry_price) * held.qty
            self._count(counts, "settled")
            self._log(
                held,
                ts,
                verdict="ok",
                final_price=final_price,
                realized_pnl=realized,
                reason="settlement",
            )
            logger.info(
                "settled position %d: %s %s qty=%.4f @ %.4f realized=%+.4f",
                held.position_id,
                held.market_id,
                held.side,
                held.qty,
                final_price,
                realized,
            )

    def _log(
        self,
        held: HeldPosition,
        ts: float,
        *,
        verdict: str,
        final_price: float | None = None,
        realized_pnl: float | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        entry = SettlementDecision(
            ts=ts,
            position_id=held.position_id,
            market_id=held.market_id,
            side=held.side,
            verdict=verdict,  # type: ignore[arg-type]
            final_price=final_price,
            realized_pnl=realized_pnl,
            reason=reason,
            error=error,
        )
        settlement_log.append(entry)
        if self._settlement_persist is not None:
            try:
                self._settlement_persist(entry)
            except Exception:  # noqa: BLE001 — a bad persist hook must not break the monitor
                logger.exception("settlement_persist raised; suppressing")


# Module-level singleton — FastAPI lifespan calls configure() + start().
settlement_monitor = SettlementMonitor()
