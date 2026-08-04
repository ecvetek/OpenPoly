"""Reconciliation monitor — closes DB-open positions the wallet no longer holds.

The exit monitor and settlement monitor both assume the DB position ledger
matches on-chain reality. It can diverge: a position is exited on-chain (sold,
redeemed, transferred) without openPoly recording the close — e.g. the on-chain
SELL filled but the DB write was dropped (pre-hardening), or a manual/external
trade. The position then sits ``status=open`` forever: the exit monitor fires
into a void (``ctf_cache_not_synced`` loop) and the UI shows fictional exposure.

The settlement monitor can't catch this — it only closes positions whose
*market* resolved (Gamma ``closed=true``); a position emptied on-chain while its
market is still active is invisible to it.

This monitor sweeps periodically, asks an injected ``holdings_fetcher`` what the
wallet actually holds on-chain (the production fetcher hits the Polymarket
data-api, which is authoritative and accounts for neg-risk wrapping), and for
any open position whose ``(condition_id, side)`` is absent from that set —
*and* older than ``grace_seconds`` (a fresh buy's settlement/indexer update can
lag) — closes it via ``close_position(reason="reconciled")``.

It also compares *quantity* for positions that are still tracked: the fetcher
returns each held pair's on-chain size, so a ledger qty that has drifted from
the on-chain size (e.g. a partial-cancel edge case, or a resting-order
remainder filling after the cancel appeared to fail) is logged loudly rather
than silently ignored — the previous set-membership-only diff was blind to
this. Like the untracked-holding case, drift is **never auto-corrected**: the
correct on-chain size is known, but not *why* it diverged, so a human decides.

Realized PnL is deliberately recorded as **0** (close at ``avg_entry_price``):
the real exit price lives on-chain but can't be reliably attributed back to a
specific openPoly position when the same market was traded more than once, so we
do not fabricate a number. The reconciled close stops the bleed; PnL truth is a
separate, manual concern.

Reuses ``settlement_log`` for observability (same close-to-match-reality shape);
``reason="reconciled"`` distinguishes its entries.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable, Literal

from openpoly.portfolio import PortfolioStore
from openpoly.runtime.section_log import SettlementDecision, settlement_log

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 300  # 5 min — divergence isn't latency-sensitive
DEFAULT_GRACE_SECONDS = 300  # don't reconcile a buy younger than this
# Looser than PortfolioStore's own 1e-6 float-equality tolerance, to absorb
# data-api rounding/serialization noise rather than false-alarming on it.
_QTY_DRIFT_EPS = 1e-4

State = Literal["stopped", "running"]

# Async callable returning, for each (condition_id, side) held on-chain, its
# on-chain size. side ∈ {"yes", "no"}.
HoldingsFetcher = Callable[[], Awaitable["dict[tuple[str, str], float]"]]


class ReconciliationMonitor:
    """Timer-driven loop that closes positions flat on-chain as ``reconciled``."""

    def __init__(
        self,
        *,
        holdings_fetcher: HoldingsFetcher,
        tick_interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        live_check: Callable[[], bool] | None = None,
    ) -> None:
        self._fetcher = holdings_fetcher
        self._tick_interval = tick_interval_seconds
        self._grace = grace_seconds
        # Safety gate: reconciliation compares DB positions against the wallet's
        # real on-chain holdings, which is only meaningful in live mode. In paper
        # mode the indexer knows nothing of paper positions, so running would
        # close them all. Default None = always run (tests); production wires
        # this to ``exec_mode == "live"``.
        self._live_check = live_check
        self._portfolio: PortfolioStore | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state: State = "stopped"
        # Reverse-diff alert dedup: (condition_id, side) pairs already flagged
        # as untracked this process lifetime — one loud alert per orphan, not
        # one per tick.
        self._alerted: set[tuple[str, str]] = set()
        # Quantity-drift alert dedup, same one-per-key-per-process shape as
        # ``_alerted`` above but for still-tracked positions whose qty diverges.
        self._drift_alerted: set[tuple[str, str]] = set()
        # Persist hook — optional, set by main.py's lifespan once the DB is up.
        # Without it this monitor's findings lived only in the in-memory ring it
        # shares with SettlementMonitor, so the untracked-holding and qty-drift
        # alerts — precisely the ones needing a human — were evicted within
        # hours and gone on restart.
        self._settlement_persist: Callable[[SettlementDecision], None] | None = None

    @property
    def state(self) -> State:
        return self._state

    def configure(self, portfolio: PortfolioStore) -> None:
        """Inject PortfolioStore — FastAPI lifespan calls once DB is up."""
        self._portfolio = portfolio

    def set_settlement_persist(self, hook: Callable[[SettlementDecision], None] | None) -> None:
        """Mirror this monitor's entries into the durable settlement_decision
        table. Same hook and same table SettlementMonitor writes to —
        ``reason`` distinguishes the source (``reconciled`` /
        ``untracked_onchain_holding`` / ``quantity_drift``)."""
        self._settlement_persist = hook

    def _append(self, entry: SettlementDecision) -> None:
        settlement_log.append(entry)
        if self._settlement_persist is not None:
            try:
                self._settlement_persist(entry)
            except Exception:  # noqa: BLE001 — a bad persist hook must not break the monitor
                logger.exception("settlement_persist raised; suppressing")

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._state = "running"
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """Signal the tick loop to stop, then await its natural exit — never
        ``task.cancel()``. A cancel mid-sweep can abandon a ``close_position``
        partway through the open-position walk. Same discipline as
        ``PipelineOrchestrator.stop``; the loop checks ``_stop`` between ticks
        and its interval wait wakes early on it."""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        self._state = "stopped"

    # ---------- loop ----------

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception:  # noqa: BLE001 — loop must survive any tick error
                logger.exception("reconciliation monitor: tick failed")
            await asyncio.sleep(0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_interval)

    # ---------- tick ----------

    async def _tick_once(self) -> None:
        if self._portfolio is None:
            return
        if self._live_check is not None and not self._live_check():
            return
        opens = self._portfolio.get_open_positions()

        try:
            held = await self._fetcher()
        except Exception as exc:  # noqa: BLE001 — network errors must not crash loop
            logger.warning("reconciliation monitor: holdings fetch failed: %s", exc)
            return

        ts = time.time()

        # Reverse diff — the wallet holds a (condition, side) the ledger has no
        # open position for (e.g. a resting GTC remainder that filled after the
        # cancel failed, or an external transfer). Alert loudly, but NEVER
        # auto-open: the cost basis is unknown and auto-created positions would
        # confuse entry dedup. A human decides what to do with it.
        known = {(p.condition_id, p.side) for p in opens}
        for cid, side in sorted(set(held) - known):
            if (cid, side) in self._alerted:
                continue
            self._alerted.add((cid, side))
            logger.warning(
                "reverse reconciliation: wallet holds UNTRACKED (%s…, %s) — "
                "no ledger position; manual review needed",
                cid[:14],
                side,
            )
            self._append(
                SettlementDecision(
                    ts=ts,
                    position_id=-1,
                    market_id=cid,
                    side=side,
                    verdict="skip",
                    final_price=None,
                    realized_pnl=None,
                    reason="untracked_onchain_holding",
                    error=None,
                )
            )

        if not opens:
            return
        for pos in opens:
            if ts - pos.opened_at < self._grace:
                continue
            key = (pos.condition_id, pos.side)
            if key in held:
                drift = abs(held[key] - pos.qty)
                if drift > _QTY_DRIFT_EPS and key not in self._drift_alerted:
                    self._drift_alerted.add(key)
                    logger.warning(
                        "reconciliation: qty drift on tracked position %d (%s…, %s) — "
                        "ledger=%.6f on-chain=%.6f; NOT auto-corrected, manual review needed",
                        pos.position_id,
                        pos.condition_id[:14],
                        pos.side,
                        pos.qty,
                        held[key],
                    )
                    self._log(
                        pos,
                        ts,
                        verdict="skip",
                        reason="quantity_drift",
                        error=f"ledger={pos.qty:.6f} onchain={held[key]:.6f}",
                    )
                continue
            # Flat on-chain but open in the DB → exited outside the ledger.
            try:
                self._portfolio.close_position(
                    pos.position_id,
                    sell_price=pos.avg_entry_price,  # realized 0 — see module docstring
                    ts=ts,
                    close_reason="reconciled",
                    trigger="reconciled",
                    order_id=None,
                    tx_hash=None,
                )
            except Exception as exc:  # noqa: BLE001 — one bad close must not abort sweep
                logger.exception(
                    "reconciliation monitor: close_position failed for %d",
                    pos.position_id,
                )
                self._log(pos, ts, verdict="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
                continue
            logger.info(
                "reconciled position %d: %s %s qty=%.4f flat on-chain → closed",
                pos.position_id,
                pos.market_id,
                pos.side,
                pos.qty,
            )
            self._log(pos, ts, verdict="ok", realized_pnl=0.0, reason="reconciled")

    def _log(
        self,
        pos,
        ts: float,
        *,
        verdict: str,
        realized_pnl: float | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        self._append(
            SettlementDecision(
                ts=ts,
                position_id=pos.position_id,
                market_id=pos.market_id,
                side=pos.side,
                verdict=verdict,  # type: ignore[arg-type]
                final_price=None,
                realized_pnl=realized_pnl,
                reason=reason,
                error=error,
            )
        )


# Module-level singleton — FastAPI lifespan calls configure() + start() when a
# wallet is present (the holdings fetcher needs the funder address).
reconciliation_monitor: ReconciliationMonitor | None = None
