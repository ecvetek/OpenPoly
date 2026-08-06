"""Tests for ExitMonitor — the timer-driven close loop.

The exit section is the real (pure) ThresholdExitV0 — TP / SL is driven by the
order-book bid price. The executor and portfolio are fakes, so the monitor's
own orchestration (mark → run → route → log) is tested in isolation.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.tables import OrderBookSnapshot
from openpoly.execution import ExecResult
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import MarketStore
from openpoly.portfolio import HeldPosition, PortfolioStore
from openpoly.runtime.exit_monitor import ExitMonitor
from openpoly.runtime.section_log import ExitDecision, exit_log
from openpoly.sections.exit.scale_out_v0 import ScaleOutExitConfig, ScaleOutExitV0
from openpoly.sections.exit.threshold_v0 import (
    ThresholdExitConfig,
    ThresholdExitV0,
)


@pytest.fixture(autouse=True)
def _isolate():
    """Fresh market catalog + exit log per test."""
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    exit_log.reset()
    yield
    market_source_manager.store = saved
    exit_log.reset()


def _held(
    position_id: int,
    token_id: str,
    *,
    avg: float = 0.40,
    market_id: str = "m1",
    side: str = "yes",
) -> HeldPosition:
    return HeldPosition(
        position_id=position_id,
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        token_id=token_id,
        condition_id=f"0x{market_id}",
        qty=20.0,
        avg_entry_price=avg,
        opened_at=1.0,
    )


def _book(token_id: str, bid: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(bid, 100.0)],
        asks=[(bid + 0.02, 100.0)],
    )


class _FakePortfolio:
    def __init__(self, positions: list[HeldPosition]) -> None:
        self._positions = positions

    def get_open_positions(self) -> list[HeldPosition]:
        return list(self._positions)


class _FakeExecutor:
    """Records execute_sell calls; returns a canned ExecResult or raises."""

    def __init__(
        self,
        *,
        result: ExecResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[dict[str, object]] = []

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: str,
        ts: float,
        trigger: str | None,
        qty: float | None = None,
    ) -> ExecResult:
        self.calls.append(
            {
                "position_id": position.position_id,
                "close_reason": close_reason,
                "trigger": trigger,
                "qty": qty,
            }
        )
        if self._exc is not None:
            raise self._exc
        return self._result or ExecResult.ok(
            price=0.55,
            qty=(qty if qty is not None else position.qty),
            position_id=position.position_id,
        )


class _BlockingExecutor:
    """Blocks in execute_sell() (on a real threading.Event) until released —
    proves _tick_loop offloads _tick_once to a worker thread via
    asyncio.to_thread (D20), not run inline (a live-broker order submit +
    fill poll would otherwise stall the event loop for the whole tick)."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: str,
        ts: float,
        trigger: str | None,
        qty: float | None = None,
    ) -> ExecResult:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return ExecResult.ok(
            price=0.55,
            qty=(qty if qty is not None else position.qty),
            position_id=position.position_id,
        )


def _monitor(portfolio: _FakePortfolio, executor: _FakeExecutor) -> ExitMonitor:
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=executor,
        tick_interval_seconds=3600,
    )
    m.configure(portfolio)  # type: ignore[arg-type]
    return m


def test_tick_skips_entirely_while_backtest_active() -> None:
    """A backtest replay swaps the live MarketStore global for historical
    data — the live exit sweep must never evaluate positions (or fire a real
    sell) while that swap is in effect (see openpoly.backtest.guard's module
    docstring)."""
    from openpoly.backtest.guard import backtest_active, set_backtest_active

    assert backtest_active() is False  # sanity: test isolation
    market_source_manager.store.set_order_books(
        [_book("t1", bid=0.55)]
    )  # would trigger take_profit
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    set_backtest_active(True)
    try:
        m._tick_once()
    finally:
        set_backtest_active(False)
    assert ex.calls == []
    assert exit_log.entries() == []


# ---------- close paths ----------


def test_take_profit_triggers_execute_sell() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor()  # default result: ExecResult.ok(price=0.55)
    _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)._tick_once()
    # (0.55 - 0.40) / 0.40 = 0.375 ≥ 0.20 → take_profit
    assert ex.calls == [
        {
            "position_id": 1,
            "close_reason": "take_profit",
            "trigger": "take_profit",
            "qty": 20.0,  # ThresholdExitV0's CloseIntent.qty is always the full remaining qty
        }
    ]
    e = exit_log.entries()[0]
    assert e.verdict == "ok"
    assert e.trigger == "take_profit"
    assert e.fill_price == 0.55
    assert e.realized_pnl == pytest.approx((0.55 - 0.40) * 20.0)


def test_stop_loss_triggers_execute_sell() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.30)])
    ex = _FakeExecutor(result=ExecResult.ok(price=0.30, qty=20.0, position_id=1))
    _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)._tick_once()
    # (0.30 - 0.40) / 0.40 = -0.25 ≤ -0.15 → stop_loss
    assert ex.calls[0]["close_reason"] == "stop_loss"
    assert exit_log.entries()[0].trigger == "stop_loss"


def test_within_thresholds_holds() -> None:
    # v18: a within-threshold hold writes NO log entry (the ring keeps only
    # ok / error closes); tick telemetry records the position was evaluated.
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    m._tick_once()
    assert ex.calls == []  # nothing closed
    assert exit_log.entries() == []  # no skip entry
    assert m.open_positions == 1
    assert m.blocked == 0
    assert m.last_tick_at is not None


def test_no_order_book_blocked() -> None:
    # v18: no order book → can't evaluate → counted as blocked, no log entry.
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t-missing")]), ex)
    m._tick_once()
    assert ex.calls == []
    assert exit_log.entries() == []
    assert m.open_positions == 1
    assert m.blocked == 1


def test_empty_bids_blocked() -> None:
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[], asks=[(0.5, 100.0)])]
    )
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1")]), ex)
    m._tick_once()
    assert ex.calls == []
    assert exit_log.entries() == []
    assert m.blocked == 1


def test_tick_telemetry_open_and_blocked() -> None:
    # One evaluable (held within thresholds) + one blocked (no order book) →
    # open=2, blocked=1, and still zero log entries.
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    positions = [_held(1, "t1", avg=0.40), _held(2, "t-missing", market_id="m2")]
    m = _monitor(_FakePortfolio(positions), ex)
    m._tick_once()
    assert m.open_positions == 2
    assert m.blocked == 1
    assert exit_log.entries() == []
    assert m.last_tick_at is not None


# ---------- error handling ----------


def test_execute_sell_raises_logged_as_error_sweep_continues() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55), _book("t2", bid=0.55)])
    ex = _FakeExecutor(exc=ValueError("position already closed"))
    positions = [
        _held(1, "t1", avg=0.40),
        _held(2, "t2", avg=0.40, market_id="m2"),
    ]
    _monitor(_FakePortfolio(positions), ex)._tick_once()
    # Both TP-trigger → execute_sell raises on both → both logged error;
    # the first error did not abort the sweep.
    entries = exit_log.entries()
    assert [e.verdict for e in entries] == ["error", "error"]
    assert {e.position_id for e in entries} == {1, 2}


def test_execute_sell_not_filled_logged_as_error() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor(result=ExecResult.skip("no_bid_liquidity"))
    _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)._tick_once()
    e = exit_log.entries()[0]
    assert e.verdict == "error"
    assert e.error is not None
    assert "no_bid_liquidity" in e.error


# ---------- no-op paths ----------


def test_empty_positions_noop() -> None:
    ex = _FakeExecutor()
    _monitor(_FakePortfolio([]), ex)._tick_once()
    assert exit_log.entries() == []
    assert ex.calls == []


def test_not_configured_noop() -> None:
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=_FakeExecutor(),
    )
    m._tick_once()  # no configure() — portfolio is None
    assert exit_log.entries() == []


# ---------- persist hook ----------


def test_persist_hook_fires_on_close() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    monitor = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), _FakeExecutor())
    persisted: list[ExitDecision] = []
    monitor.set_exit_persist(persisted.append)
    monitor._tick_once()
    assert [d.trigger for d in persisted] == ["take_profit"]


def test_raising_persist_hook_is_swallowed() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    monitor = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), _FakeExecutor())

    def _boom(_decision) -> None:
        raise RuntimeError("persist boom")

    monitor.set_exit_persist(_boom)
    monitor._tick_once()  # must not raise
    # The ring still got its entry despite the persist hook raising.
    assert len(exit_log.entries()) == 1


def test_persist_hook_cleared_via_none() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    monitor = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), _FakeExecutor())
    persisted: list[ExitDecision] = []
    monitor.set_exit_persist(persisted.append)
    monitor.set_exit_persist(None)
    monitor._tick_once()
    assert persisted == []


# ---------- loop lifecycle ----------


async def test_tick_loop_start_stop() -> None:
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    assert m.state == "stopped"
    await m.start()
    assert m.state == "running"
    await asyncio.sleep(0)  # let the loop run one iteration
    await m.stop()
    assert m.state == "stopped"


async def test_stop_before_start_is_safe() -> None:
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    await m.stop()  # never started
    assert m.state == "stopped"


# ---------- canvas-driven tick_interval_seconds (hot-swap + startup) ----------


async def test_replace_exit_section_updates_tick_interval() -> None:
    """tick_interval_seconds now lives on the same ThresholdExitConfig as the
    thresholds — swapping in a new section (canvas save while running, or
    the lifespan's startup load) must update the monitor's tick cadence too,
    not just the threshold values."""
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    assert m._tick_interval == 3600  # _monitor()'s constructor arg
    new_section = ThresholdExitV0(ThresholdExitConfig(tick_interval_seconds=45))
    await m.replace_exit_section(new_section)
    assert m._tick_interval == 45


async def test_replace_exit_section_keeps_tick_interval_when_new_section_has_no_config() -> None:
    """A hypothetical exit-section implementation with no config.tick_interval_seconds
    (the _ExitSection Protocol only requires .run()) must not crash or reset
    the cadence — the monitor just keeps whatever it already had."""
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    assert m._tick_interval == 3600

    class _NoConfigSection:
        def run(self, input):  # noqa: A002
            raise NotImplementedError

    await m.replace_exit_section(_NoConfigSection())
    assert m._tick_interval == 3600  # unchanged


async def test_tick_loop_offloads_slow_execute_sell_without_blocking() -> None:
    """A slow execute_sell (e.g. a live-broker order submit + fill poll) must
    not stall the event loop — _tick_loop offloads _tick_once via
    asyncio.to_thread specifically so other concurrently-scheduled coroutines
    keep making progress during it (D20)."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    executor = _BlockingExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), executor)  # type: ignore[arg-type]
    await m.start()
    try:
        await asyncio.to_thread(executor.started.wait, 5)

        # If _tick_once ran inline on the event loop, this concurrently
        # scheduled sleep loop could never make progress until it returned.
        ticks = 0
        for _ in range(5):
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=1)
            ticks += 1
        assert ticks == 5

        executor.release.set()
    finally:
        executor.release.set()
        await m.stop()
    assert executor.calls == 1


def test_partial_fill_marks_realized_against_filled_qty() -> None:
    """A live GTC sell can partially fill; record_sell then keeps the position
    open with the remainder. Marking against the *intended* qty overstated the
    log entry (and the PositionDetail UI) on every partial close, while the
    DB's own realized_pnl was computed correctly from the filled qty."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    # Position is 20 shares at 0.40; only 5 fill at 0.55.
    executor = _FakeExecutor(result=ExecResult.ok(price=0.55, qty=5.0, position_id=1))
    monitor = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), executor)
    monitor._tick_once()

    entries = exit_log.entries()
    assert len(entries) == 1
    assert entries[0].verdict == "ok"
    assert entries[0].realized_pnl == pytest.approx((0.55 - 0.40) * 5.0)


async def test_stop_waits_for_in_flight_sell_to_persist() -> None:
    """``stop()`` must not return while a sell is still in flight.

    The tick body runs on a worker thread via ``asyncio.to_thread``, so
    ``task.cancel()`` would return immediately while the thread kept going
    through execute_sell → _log → _exit_persist. The lifespan tears down the DB
    writer right after stop() returns, so a cancel means an on-chain fill whose
    ledger record lands in a torn-down writer — or nowhere.
    """
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    executor = _BlockingExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), executor)  # type: ignore[arg-type]
    persisted: list[ExitDecision] = []
    m.set_exit_persist(persisted.append)

    await m.start()
    await asyncio.to_thread(executor.started.wait, 5)
    assert persisted == []  # sell is blocked mid-flight

    stopping = asyncio.create_task(m.stop())
    # Give stop() every chance to return early if it were cancelling.
    for _ in range(5):
        await asyncio.sleep(0.01)
    assert not stopping.done(), "stop() returned while a sell was still in flight"

    executor.release.set()
    await asyncio.wait_for(stopping, timeout=5)

    assert m.state == "stopped"
    assert len(persisted) == 1, "the in-flight tick's persist hook must have run"
    assert persisted[0].verdict == "ok"


# ---------- peak tracking ----------


def test_peak_persists_across_ticks_and_triggers_drawdown() -> None:
    # TP set very high so peak_drawdown is the *only* close trigger that can
    # fire over the price path 0.46 → 0.50 → 0.47 with entry 0.40.
    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(
            ThresholdExitConfig(
                take_profit_pct=0.50,
                stop_loss_pct=0.50,
                peak_drawdown_pct=0.12,
            )
        ),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    portfolio = _FakePortfolio([_held(1, "t1", avg=0.40)])
    monitor.configure(portfolio)  # type: ignore[arg-type]
    store = market_source_manager.store

    # Tick 1: bid 0.46 → +15%, no trigger; peak seeded at 0.46.
    # v18: a hold writes no log entry — assert via the peak instead.
    store.set_order_books([_book("t1", bid=0.46)])
    monitor._tick_once()
    assert monitor._peak[1] == pytest.approx(0.46)
    assert exit_log.entries() == []

    # Tick 2: bid climbs to 0.50; peak follows.
    store.set_order_books([_book("t1", bid=0.50)])
    monitor._tick_once()
    assert monitor._peak[1] == pytest.approx(0.50)
    assert exit_log.entries() == []

    # Tick 3: bid retreats to 0.47; peak stays at 0.50.
    # peak_dd = (0.50 - 0.47) / (0.50 - 0.40) = 0.30 ≥ 0.12 → close.
    store.set_order_books([_book("t1", bid=0.47)])
    monitor._tick_once()
    last = exit_log.entries()[-1]
    assert last.verdict == "ok"
    assert last.trigger == "peak_drawdown"
    assert last.peak_price == pytest.approx(0.50)
    # On a successful close the peak entry is dropped.
    assert 1 not in monitor._peak


def test_peak_entry_dropped_when_position_closed_externally() -> None:
    """A position can close via a path other than this monitor's own
    _evaluate() (SettlementMonitor, manual close, reconciliation) — the next
    tick must garbage-collect its now-stale peak entry, or _peak would grow
    unboundedly for the life of the process."""
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    # Simulate a stale entry left behind by a position no longer open.
    m._peak[999] = 0.5
    m._tick_once()
    assert 999 not in m._peak


def test_peak_entry_kept_for_still_open_position() -> None:
    """Sanity check the self-heal doesn't accidentally drop a live entry."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), _FakeExecutor())
    m._peak[1] = 0.41
    m._tick_once()
    assert 1 in m._peak


def test_peak_tracked_on_hold() -> None:
    # v18: held within thresholds writes no entry, but the peak is still
    # tracked in-memory for the drawdown trigger.
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    m._tick_once()
    assert exit_log.entries() == []
    assert m._peak[1] == pytest.approx(0.41)


def test_bootstrap_peaks_rebuilds_from_snapshots(tmp_path) -> None:
    db_path = tmp_path / "openpoly_peak.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    sf = make_session_factory(engine)

    pf = PortfolioStore(sf)
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )

    # Three snapshots after opened_at; peak bid = 0.55. One snapshot *before*
    # opened_at with a higher bid that must be ignored.
    with sf() as session:
        session.add_all(
            [
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=99.0,  # before open — ignored
                    bids_json=json.dumps([[0.99, 100]]),
                    asks_json=json.dumps([[1.0, 100]]),
                ),
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=110.0,
                    bids_json=json.dumps([[0.45, 100]]),
                    asks_json=json.dumps([[0.46, 100]]),
                ),
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=120.0,
                    bids_json=json.dumps([[0.55, 100]]),
                    asks_json=json.dumps([[0.56, 100]]),
                ),
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=130.0,
                    bids_json=json.dumps([[0.50, 100]]),
                    asks_json=json.dumps([[0.51, 100]]),
                ),
            ]
        )
        session.commit()

    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(pf)
    monitor.bootstrap_peaks(sf)
    assert monitor._peak[held.position_id] == pytest.approx(0.55)


def test_bootstrap_peaks_no_snapshot_falls_back_to_entry(tmp_path) -> None:
    db_path = tmp_path / "openpoly_peak2.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    sf = make_session_factory(engine)

    pf = PortfolioStore(sf)
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )

    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(pf)
    monitor.bootstrap_peaks(sf)
    # No snapshots after opened_at → peak defaults to avg_entry_price (0.40).
    assert monitor._peak[held.position_id] == pytest.approx(0.40)


# ---------- scale-out exit: qty threading + _scaled_out lifecycle ----------


def test_scale_out_fill_sets_scaled_out_and_keeps_peak_tracked() -> None:
    """A scale-out fill (partial — the remainder stays open) must mark
    self._scaled_out[position_id] and must NOT pop self._peak: the remainder
    still needs peak tracking for its own peak_drawdown check."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.65)])
    # ScaleOutExitConfig defaults: scale_out_trigger_pct=0.20, fraction=0.5.
    # (0.65-0.40)/0.40 = 0.625 ≥ 0.20 → scale_out, qty = 20 * 0.5 = 10.
    ex = _FakeExecutor(result=ExecResult.ok(price=0.65, qty=10.0, position_id=1))
    monitor = ExitMonitor(
        exit_section=ScaleOutExitV0(ScaleOutExitConfig()),
        executor=ex,
        tick_interval_seconds=3600,
    )
    monitor.configure(_FakePortfolio([_held(1, "t1", avg=0.40)]))  # qty=20
    monitor._tick_once()

    assert ex.calls[0]["trigger"] == "scale_out"
    assert ex.calls[0]["qty"] == pytest.approx(10.0)
    assert monitor._scaled_out[1] is True
    assert 1 in monitor._peak  # NOT dropped — position is still open
    e = exit_log.entries()[0]
    assert e.verdict == "ok"
    assert e.trigger == "scale_out"


def test_scaled_out_position_evaluates_post_scale_out_rules() -> None:
    """Once _scaled_out[position_id] is set, the section must be fed
    MarkedPosition.scaled_out=True — proven by a return that would hold
    under the pre-scale-out rules (no stop_loss at -2%) but closes under
    the post-scale-out breakeven stop (default 0.0)."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.39)])  # -2.5%
    remainder = HeldPosition(
        position_id=1,
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=10.0,  # already reduced by a prior scale-out
        avg_entry_price=0.40,
        opened_at=1.0,
    )
    ex = _FakeExecutor(result=ExecResult.ok(price=0.39, qty=10.0, position_id=1))
    monitor = ExitMonitor(
        exit_section=ScaleOutExitV0(ScaleOutExitConfig()),
        executor=ex,
        tick_interval_seconds=3600,
    )
    monitor.configure(_FakePortfolio([remainder]))
    monitor._scaled_out[1] = True  # simulate a scale-out that already happened

    monitor._tick_once()

    assert ex.calls[0]["trigger"] == "post_scale_out_stop"
    assert ex.calls[0]["qty"] == pytest.approx(10.0)


def test_post_scale_out_full_close_pops_both_peak_and_scaled_out() -> None:
    """A full close of the (already scaled-out) remainder must pop BOTH
    self._peak and self._scaled_out — the position is actually gone, so
    neither should linger for a future position_id."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.39)])
    remainder = HeldPosition(
        position_id=1,
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=10.0,
        avg_entry_price=0.40,
        opened_at=1.0,
    )
    ex = _FakeExecutor(result=ExecResult.ok(price=0.39, qty=10.0, position_id=1))
    monitor = ExitMonitor(
        exit_section=ScaleOutExitV0(ScaleOutExitConfig()),
        executor=ex,
        tick_interval_seconds=3600,
    )
    monitor.configure(_FakePortfolio([remainder]))
    monitor._scaled_out[1] = True
    monitor._peak[1] = 0.50

    monitor._tick_once()

    assert 1 not in monitor._scaled_out
    assert 1 not in monitor._peak


def test_scaled_out_pruned_when_position_closes_via_other_path() -> None:
    """A position that scaled-out and then closed via settlement / manual /
    reconciliation (not this monitor) must have its _scaled_out entry
    pruned by the same self-heal loop that already prunes _peak — otherwise
    it leaks for the life of the process."""
    monitor = ExitMonitor(
        exit_section=ScaleOutExitV0(ScaleOutExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(_FakePortfolio([]))  # no open positions this sweep
    monitor._scaled_out[42] = True  # stale — position 42 closed elsewhere
    monitor._peak[42] = 0.60

    monitor._tick_once()

    assert 42 not in monitor._scaled_out
    assert 42 not in monitor._peak


def test_bootstrap_scaled_out_seeds_from_prior_scale_out_fill(tmp_path) -> None:
    db_path = tmp_path / "openpoly_scaled_out.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    sf = make_session_factory(engine)

    pf = PortfolioStore(sf)
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )
    # Simulate a scale-out that already happened before this restart.
    pf.record_sell(
        held.position_id,
        sold_qty=10.0,
        sell_price=0.65,
        ts=150.0,
        close_reason="scale_out",
        trigger="scale_out",
    )

    monitor = ExitMonitor(
        exit_section=ScaleOutExitV0(ScaleOutExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(pf)
    monitor.bootstrap_scaled_out()

    assert monitor._scaled_out[held.position_id] is True


def test_bootstrap_scaled_out_no_prior_fill_does_not_seed(tmp_path) -> None:
    db_path = tmp_path / "openpoly_no_scaled_out.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    sf = make_session_factory(engine)

    pf = PortfolioStore(sf)
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )

    monitor = ExitMonitor(
        exit_section=ScaleOutExitV0(ScaleOutExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(pf)
    monitor.bootstrap_scaled_out()

    assert held.position_id not in monitor._scaled_out
