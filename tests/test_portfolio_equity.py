"""Tests for build_equity_curve — equity-curve reconstruction (Portfolio Overview)."""

from __future__ import annotations

import json

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.tables import OrderBookSnapshot
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.equity import build_equity_curve


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/equity.db")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _snapshot(factory, token_id: str, recorded_at: float, bid: float | None) -> None:
    """Persist one order-book snapshot. ``bid is None`` writes an empty book."""
    with factory() as s:
        s.add(
            OrderBookSnapshot(
                token_id=token_id,
                recorded_at=recorded_at,
                bids_json=json.dumps([] if bid is None else [[bid, 100.0]]),
                asks_json=json.dumps([[0.99, 100.0]]),
            )
        )
        s.commit()


def test_empty_ledger_returns_empty_curve(factory) -> None:
    curve = build_equity_curve(factory)
    assert curve.points == ()
    assert curve.realized == 0.0
    assert curve.unrealized == 0.0
    assert curve.total == 0.0
    assert curve.open_positions == 0


def test_open_position_no_snapshots_marks_at_entry(factory) -> None:
    PortfolioStore(factory).open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    curve = build_equity_curve(factory)
    # No snapshot → mark == entry → unrealized 0 at every point.
    assert curve.unrealized == 0.0
    assert curve.realized == 0.0
    assert curve.open_positions == 1
    assert all(p.unrealized == 0.0 for p in curve.points)


def test_open_position_unrealized_tracks_best_bid(factory) -> None:
    PortfolioStore(factory).open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    _snapshot(factory, "ty1", 150.0, 0.50)  # (0.50 - 0.40) * 10 = +1.00
    curve = build_equity_curve(factory)
    assert curve.unrealized == pytest.approx(1.00)
    assert curve.total == pytest.approx(1.00)
    pt = next(p for p in curve.points if p.ts == 150.0)
    assert pt.unrealized == pytest.approx(1.00)


def test_closed_position_realized_step(factory) -> None:
    store = PortfolioStore(factory)
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    store.close_position(
        held.position_id,
        sell_price=0.55,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    curve = build_equity_curve(factory)
    assert curve.realized == pytest.approx(1.50)  # (0.55 - 0.40) * 10
    assert curve.unrealized == 0.0
    assert curve.open_positions == 0
    before = [p for p in curve.points if p.ts < 200.0]
    after = [p for p in curve.points if p.ts >= 200.0]
    assert all(p.realized == 0.0 for p in before)
    assert all(p.realized == pytest.approx(1.50) for p in after)


def test_mix_open_and_closed(factory) -> None:
    store = PortfolioStore(factory)
    h1 = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    store.close_position(
        h1.position_id,
        sell_price=0.50,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    store.open_position(
        market_id="m2",
        side="no",
        token_id="tn2",
        condition_id="0xc2",
        price=0.30,
        qty=20.0,
        ts=300.0,
        news_id="n2",
    )
    _snapshot(factory, "tn2", 350.0, 0.35)  # (0.35 - 0.30) * 20 = +1.00
    curve = build_equity_curve(factory)
    assert curve.realized == pytest.approx(1.00)
    assert curve.unrealized == pytest.approx(1.00)
    assert curve.total == pytest.approx(2.00)
    assert curve.open_positions == 1


def test_snapshot_with_empty_bids_is_skipped(factory) -> None:
    PortfolioStore(factory).open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    _snapshot(factory, "ty1", 150.0, 0.50)
    _snapshot(factory, "ty1", 160.0, None)  # empty bids → ignored, mark held
    curve = build_equity_curve(factory)
    assert curve.unrealized == pytest.approx(1.00)


def test_same_token_closed_then_reopened(factory) -> None:
    store = PortfolioStore(factory)
    h1 = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    store.close_position(
        h1.position_id,
        sell_price=0.50,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.45,
        qty=10.0,
        ts=300.0,
        news_id="n2",
    )
    _snapshot(factory, "ty1", 350.0, 0.48)  # (0.48 - 0.45) * 10 = +0.30
    curve = build_equity_curve(factory)
    assert curve.realized == pytest.approx(1.00)
    assert curve.unrealized == pytest.approx(0.30)
    assert curve.open_positions == 1


# ---------- windowing (window_seconds / now) ----------


def test_window_excludes_position_closed_before_window(factory) -> None:
    store = PortfolioStore(factory)
    h = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=50.0,
        news_id="n1",
    )
    store.close_position(
        h.position_id,
        sell_price=0.55,
        ts=100.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    # now=500, window=200 -> since=300; position closed at 100 (< since) is
    # dropped entirely from the windowed curve.
    curve = build_equity_curve(factory, window_seconds=200.0, now=500.0)
    assert curve.points == ()
    assert curve.realized == 0.0
    assert curve.open_positions == 0


def test_window_includes_open_position_regardless_of_open_age(factory) -> None:
    store = PortfolioStore(factory)
    store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=10.0,  # well before the window
        news_id="n1",
    )
    curve = build_equity_curve(factory, window_seconds=200.0, now=500.0)
    assert curve.open_positions == 1
    assert curve.points[0].ts == 300.0  # clamped to `since` — the window's left edge
    assert curve.points[-1].ts == 500.0


def test_window_boundary_seed_marks_position_open_before_window(factory) -> None:
    """The critical seed-row test: without the pre-window boundary snapshot,
    the point at t == since would read unrealized == 0 (fallback to entry
    price) instead of marking at the last real price before the window."""
    store = PortfolioStore(factory)
    store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=50.0,
        news_id="n1",
    )
    _snapshot(factory, "ty1", 80.0, 0.45)  # before since=300 -> boundary seed
    _snapshot(factory, "ty1", 350.0, 0.50)  # inside the window
    curve = build_equity_curve(factory, window_seconds=200.0, now=500.0)
    pt_at_since = next(p for p in curve.points if p.ts == 300.0)
    assert pt_at_since.unrealized == pytest.approx((0.45 - 0.40) * 10.0)


# ---------- _advance_mark (two-pointer mark lookup) ----------


def _naive_mark_at(marks, t):
    """Reference reimplementation of the old (now-deleted) _mark_at — a
    plain linear rescan from the start, for equivalence testing."""
    result = None
    for ts, bid in marks:
        if ts <= t:
            result = bid
        else:
            break
    return result


@pytest.mark.parametrize(
    "marks,timeline",
    [
        ([], [0.0, 10.0]),
        ([(5.0, 1.0)], [0.0, 5.0, 5.0, 10.0]),
        ([(5.0, 1.0), (5.0, 2.0)], [4.0, 5.0, 6.0]),  # duplicate timestamps
        ([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)], [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]),
    ],
)
def test_advance_mark_matches_naive_scan(marks, timeline) -> None:
    from openpoly.portfolio.equity import _advance_mark

    cursor = -1
    for t in timeline:
        got, cursor = _advance_mark(marks, cursor, t)
        assert got == _naive_mark_at(marks, t)
