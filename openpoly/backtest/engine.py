"""Backtest/replay engine — runs a candidate entry+exit section config
against PERSISTED history (``AnalyzerCallRow`` + ``OrderBookSnapshot``),
reusing the real section code, the real ``PaperExecutor`` fill model, and
the real ``aggregate_closed_positions`` P&L math. Never touches the real
``position`` / ``fill`` tables — everything lands in an in-memory
``BacktestPortfolio`` instead.

Three scoping notes, stated plainly rather than buried in inline comments:

- Because entry replay is driven off already-decided ``AnalyzerCallRow``
  rows (``verdict='ok'``), this answers "given the analyzer's historical
  calls, how would a different entry/exit config have performed" — it does
  NOT re-run the LLM, and cannot recover analyzer calls a different
  historical ``min_confidence`` would have filtered out.
- Market metadata isn't persisted anywhere (see ``historical_store.py``) —
  this replays historical price data against the CURRENT market catalog's
  identity (token ids, tradeable, condition_id), snapshotted once at
  run-start. A historical ``market_id`` no longer in that snapshot is
  unresolvable and is counted (``skipped_market_not_in_catalog``), not
  silently dropped.
- The A4 kill-switch's *daily* time-window brake (``kill_daily_loss_usd``)
  reads wall-clock ``time.time()`` internally
  (``edge_threshold_v0._kill_switch_check``'s ``now=None`` default) rather
  than the simulated replay clock — those sections don't expose an
  injectable clock, and adding one would mean editing the baseline files
  this whole effort deliberately avoided touching. Kill switches default to
  disabled (0), so this only matters for a backtest config that opts into
  ``kill_daily_loss_usd`` specifically; ``kill_max_consecutive_losses`` and
  ``kill_max_drawdown_usd`` are trade-ordered, not time-windowed, and are
  unaffected.
- The live orchestrator and exit monitor run continuously with no pause
  control anywhere in the API (confirmed while wiring this up — see
  ``guard.py``'s module docstring). ``run_backtest`` holds
  ``guard.backtest_active()`` for the swap window instead; the orchestrator's
  ``_run_entry`` and the exit monitor's ``_tick_once`` check it and skip
  their own decision paths for that (typically brief) duration, so a real
  trading decision can never be made off the swapped historical store.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from openpoly.backtest.guard import backtest_active, set_backtest_active
from openpoly.backtest.historical_store import HistoricalMarketStore
from openpoly.backtest.portfolio import BacktestPortfolio
from openpoly.db.history_query import analyzer_calls_in_range, order_book_snapshots_for_token
from openpoly.execution.executor import PaperExecutor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.portfolio.statistics import StatisticsResult, aggregate_closed_positions
from openpoly.sections._base import SectionInput
from openpoly.sections._registry import resolve_impl
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry import conviction_sized_v0, edge_threshold_v0
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent
from openpoly.sections.exit.threshold_v0 import CloseIntent, MarkedPosition

_QTY_EPS = 1e-6  # mirrors portfolio.store._QTY_EPS / runtime.exit_monitor._QTY_EPS

# The late-buy veto's recent_move is a raw Polymarket API call, not routed
# through MarketStore — swapping the store global doesn't intercept it.
# Stubbed to None (not skipped) for the duration of a replay so a
# veto_enabled config fails open exactly like a live fetch failure would,
# rather than making real network calls against CURRENT price history
# mid-replay of the past.
_VETO_PATCH_TARGETS = (edge_threshold_v0, conviction_sized_v0)


@dataclass(frozen=True)
class BacktestRequest:
    since: float
    until: float
    entry_module: str
    entry_name: str
    entry_config: dict
    exit_module: str
    exit_name: str
    exit_config: dict


@dataclass(frozen=True)
class BacktestResult:
    statistics: StatisticsResult
    replayed_analyzer_calls: int
    skipped_market_not_in_catalog: int
    elapsed_ms: int


def run_backtest(
    request: BacktestRequest, session_factory: sessionmaker[Session]
) -> BacktestResult:
    """Resolve the requested entry/exit impls, replay entries then exits
    against persisted history, and aggregate the result.

    Swaps the live ``MarketStore`` global for a ``HistoricalMarketStore`` for
    the duration (restored in ``finally`` even if replay raises), and holds
    ``guard.backtest_active()`` for that same window. The live orchestrator
    and exit monitor run continuously with no pause control (see
    ``guard.py``'s module docstring for why) — they check that flag and skip
    their own decision paths for the duration instead, so a real trading
    decision can never be made off the swapped historical store. Raises
    ``RuntimeError`` if a backtest is already in progress (the guard, and
    the module-global store swap, aren't reentrant).
    """
    if backtest_active():
        raise RuntimeError("a backtest is already in progress")

    start = time.monotonic()

    entry_cls = resolve_impl("entry", request.entry_module, request.entry_name)
    exit_cls = resolve_impl("exit", request.exit_module, request.exit_name)
    entry_config = entry_cls.Config(**request.entry_config)
    exit_config = exit_cls.Config(**request.exit_config)

    backtest_portfolio = BacktestPortfolio()
    # portfolio_provider isn't part of the Section Protocol (only
    # Config/run() are) — a resolved entry impl isn't guaranteed to accept
    # it, same guard orchestrator.py/canvas_routes.py/main.py already use.
    try:
        entry_section = entry_cls(entry_config, portfolio_provider=lambda: backtest_portfolio)
    except TypeError:
        entry_section = entry_cls(entry_config)
    exit_section = exit_cls(exit_config)

    live_markets = {m.market_id: m for m in market_source_manager.store.snapshot()}

    original_store = market_source_manager.store
    original_recent_move = {mod: mod.recent_move for mod in _VETO_PATCH_TARGETS}
    executor = PaperExecutor(backtest_portfolio)
    set_backtest_active(True)
    with session_factory() as session:
        historical_store = HistoricalMarketStore(session, live_markets)
        market_source_manager.store = historical_store
        for mod in _VETO_PATCH_TARGETS:
            mod.recent_move = lambda token_id, *, window_min: None
        try:
            replayed, skipped = _replay_entries(
                session, request, entry_section, executor, historical_store
            )
            _replay_exits(
                session, request, exit_section, executor, backtest_portfolio, historical_store
            )
        finally:
            market_source_manager.store = original_store
            for mod, fn in original_recent_move.items():
                mod.recent_move = fn
            set_backtest_active(False)

    closed = backtest_portfolio.all_closed_ascending()
    still_open = len(backtest_portfolio.get_open_positions())
    statistics = aggregate_closed_positions(
        closed,
        positions_opened=len(closed) + still_open,
        since=request.since,
        until=request.until,
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return BacktestResult(
        statistics=statistics,
        replayed_analyzer_calls=replayed,
        skipped_market_not_in_catalog=skipped,
        elapsed_ms=elapsed_ms,
    )


def _replay_entries(
    session: Session,
    request: BacktestRequest,
    entry_section: object,
    executor: PaperExecutor,
    historical_store: HistoricalMarketStore,
) -> tuple[int, int]:
    """Walk verdict='ok' AnalyzerCallRow rows chronologically, reconstruct
    each as an AnalysisResult, and run the real entry section against it —
    mirroring what PipelineOrchestrator._run_entry does live, minus the
    news/embedding/analyzer stages this replay skips entirely. Returns
    (replayed_count, skipped_market_not_in_catalog_count)."""
    replayed = 0
    skipped = 0
    for row in analyzer_calls_in_range(session, request.since, request.until, verdict="ok"):
        if row.market_id is None or row.p_model is None or row.confidence is None:
            continue  # defensive — an "ok" verdict should always carry these
        if historical_store.get(row.market_id) is None:
            skipped += 1
            continue
        replayed += 1
        historical_store.set_clock(row.ts)
        analysis = AnalysisResult(
            market_id=row.market_id,
            p_model=row.p_model,
            confidence=row.confidence,  # type: ignore[arg-type]
            rationale=row.rationale or "",
        )
        out = entry_section.run(SectionInput(tick_type="event", payload=analysis))
        if out.verdict == "ok" and isinstance(out.payload, OrderIntent):
            executor.execute_buy(out.payload, news_id=row.news_id, ts=row.ts)
    return replayed, skipped


def _replay_exits(
    session: Session,
    request: BacktestRequest,
    exit_section: object,
    executor: PaperExecutor,
    backtest_portfolio: BacktestPortfolio,
    historical_store: HistoricalMarketStore,
) -> None:
    """For each position opened during entry replay, walk its own token's
    order-book history chronologically and evaluate the real exit section
    once per snapshot row — finer-grained than any fixed tick cadence, since
    it only ever visits timestamps the data actually changed at, and cannot
    miss a threshold crossing the live 30s loop would have caught. Mirrors
    ExitMonitor._evaluate's peak-tracking / scaled_out-injection exactly,
    but as local dicts scoped to this one replay rather than the live
    monitor's process-lifetime state."""
    peaks: dict[int, float] = {}
    scaled_out: dict[int, bool] = {}
    for opened in list(backtest_portfolio.get_open_positions()):
        rows = order_book_snapshots_for_token(
            session, opened.token_id, opened.opened_at, request.until
        )
        for row in rows:
            held = backtest_portfolio.get_open_position(opened.market_id, opened.side)
            if held is None:
                break  # already fully closed by an earlier row
            try:
                bids = json.loads(row.bids_json)
            except (TypeError, ValueError):
                continue
            if not bids:
                continue
            current_price = float(bids[0][0])
            prev_peak = peaks.get(held.position_id, current_price)
            peak_price = max(prev_peak, current_price)
            peaks[held.position_id] = peak_price

            marked = MarkedPosition(
                market_id=held.market_id,
                side=held.side,
                avg_entry_price=held.avg_entry_price,
                qty=held.qty,
                current_price=current_price,
                peak_price=peak_price,
                scaled_out=scaled_out.get(held.position_id, False),
            )
            out = exit_section.run(SectionInput(tick_type="hard", payload=marked))
            if out.verdict != "ok" or not isinstance(out.payload, CloseIntent):
                continue
            intent = out.payload
            historical_store.set_clock(row.recorded_at)
            result = executor.execute_sell(
                held,
                close_reason=intent.trigger,
                ts=row.recorded_at,
                trigger=intent.trigger,
                qty=intent.qty,
            )
            if result.filled and result.qty is not None:
                position_closed = result.qty >= held.qty - _QTY_EPS
                if position_closed:
                    peaks.pop(held.position_id, None)
                    scaled_out.pop(held.position_id, None)
                elif intent.trigger == "scale_out":
                    scaled_out[held.position_id] = True
