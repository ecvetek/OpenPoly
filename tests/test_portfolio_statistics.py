"""Tests for build_statistics — realized trading-performance aggregation
(Statistics page)."""

from __future__ import annotations

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.statistics import build_statistics


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/statistics.db")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _open(store, market_id, ts, price=0.40, qty=10.0):
    return store.open_position(
        market_id=market_id,
        side="yes",
        token_id=f"t{market_id}",
        condition_id=f"0x{market_id}",
        price=price,
        qty=qty,
        ts=ts,
    )


def test_empty_range_returns_zeroed_summary(factory) -> None:
    result = build_statistics(factory)
    assert result.since is None
    assert result.until is None
    s = result.summary
    assert s.positions_opened == 0
    assert s.positions_closed == 0
    assert s.wins == 0
    assert s.losses == 0
    assert s.breakeven == 0
    assert s.win_rate is None
    assert s.gross_profit == 0.0
    assert s.gross_loss == 0.0
    assert s.net_pnl == 0.0
    assert s.profit_factor is None
    assert s.average_win is None
    assert s.average_loss is None
    assert s.largest_win is None
    assert s.largest_loss is None
    assert s.average_hold_seconds is None
    assert s.close_reason_breakdown == {}
    assert result.pnl_curve == ()
    assert result.closed_positions == ()
    assert result.closed_positions_truncated is False


def test_open_position_counts_toward_opened_not_closed(factory) -> None:
    store = PortfolioStore(factory)
    _open(store, "m1", ts=100.0)
    result = build_statistics(factory)
    assert result.summary.positions_opened == 1
    assert result.summary.positions_closed == 0
    assert result.closed_positions == ()


def test_only_wins_in_range(factory) -> None:
    store = PortfolioStore(factory)
    h1 = _open(store, "m1", ts=100.0, price=0.40)
    store.close_position(h1.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
    h2 = _open(store, "m2", ts=110.0, price=0.30, qty=5.0)
    store.close_position(h2.position_id, sell_price=0.60, ts=210.0, close_reason="take_profit")

    s = build_statistics(factory).summary
    assert s.wins == 2
    assert s.losses == 0
    assert s.win_rate == 1.0
    assert s.gross_loss == 0.0
    assert s.profit_factor is None  # gross_loss == 0 -> None, not infinity


def test_only_losses_in_range(factory) -> None:
    store = PortfolioStore(factory)
    h1 = _open(store, "m1", ts=100.0, price=0.40)
    store.close_position(h1.position_id, sell_price=0.25, ts=200.0, close_reason="stop_loss")

    s = build_statistics(factory).summary
    assert s.wins == 0
    assert s.losses == 1
    assert s.win_rate == 0.0
    assert s.gross_profit == 0.0
    assert s.profit_factor == 0.0  # gross_loss > 0, gross_profit == 0 -> 0.0, NOT None


def test_mixed_wins_losses_breakeven(factory) -> None:
    store = PortfolioStore(factory)
    for i, sell in enumerate([0.55, 0.50]):  # 2 wins
        h = _open(store, f"win{i}", ts=100.0 + i, price=0.40)
        store.close_position(h.position_id, sell_price=sell, ts=200.0 + i, close_reason="take_profit")
    h = _open(store, "loss0", ts=103.0, price=0.40)  # 1 loss
    store.close_position(h.position_id, sell_price=0.25, ts=203.0, close_reason="stop_loss")
    h = _open(store, "be0", ts=104.0, price=0.40)  # 1 breakeven
    store.close_position(h.position_id, sell_price=0.40, ts=204.0, close_reason="manual")

    s = build_statistics(factory).summary
    assert s.wins == 2
    assert s.losses == 1
    assert s.breakeven == 1
    assert s.positions_closed == 4
    # win_rate excludes breakeven from the denominator: 2 / (2+1), not 2/4.
    assert s.win_rate == pytest.approx(2 / 3)


def test_breakeven_realized_pnl_exactly_zero(factory) -> None:
    store = PortfolioStore(factory)
    h = _open(store, "m1", ts=100.0, price=0.40)
    store.close_position(h.position_id, sell_price=0.40, ts=200.0, close_reason="manual")
    s = build_statistics(factory).summary
    assert s.breakeven == 1
    assert s.wins == 0
    assert s.losses == 0


def test_average_loss_and_largest_loss_are_signed_not_magnitude(factory) -> None:
    store = PortfolioStore(factory)
    h1 = _open(store, "m1", ts=100.0, price=0.40)
    store.close_position(h1.position_id, sell_price=0.25, ts=200.0, close_reason="stop_loss")  # -1.50
    h2 = _open(store, "m2", ts=101.0, price=0.50)
    store.close_position(h2.position_id, sell_price=0.20, ts=201.0, close_reason="stop_loss")  # -3.00

    s = build_statistics(factory).summary
    assert s.average_loss == pytest.approx(-2.25)  # signed mean, not a positive magnitude
    assert s.largest_loss == pytest.approx(-3.00)  # most negative, not abs-largest
    assert s.gross_loss == pytest.approx(4.50)  # magnitude — this one IS positive, by design


def test_positions_opened_scoped_by_opened_at_not_closed_at(factory) -> None:
    """Opened inside [100,200), closed well outside it -> counts toward
    positions_opened only; the win/loss/pnl summary (closed_at-scoped) must
    not see it."""
    store = PortfolioStore(factory)
    h = _open(store, "m1", ts=150.0)
    store.close_position(h.position_id, sell_price=0.55, ts=500.0, close_reason="take_profit")

    s = build_statistics(factory, since=100.0, until=200.0).summary
    assert s.positions_opened == 1
    assert s.positions_closed == 0


def test_closed_set_scoped_by_closed_at_not_opened_at(factory) -> None:
    """Opened well before [300,400), closed inside it -> counts toward
    positions_closed (and feeds win/loss) but not positions_opened."""
    store = PortfolioStore(factory)
    h = _open(store, "m1", ts=50.0)
    store.close_position(h.position_id, sell_price=0.55, ts=350.0, close_reason="take_profit")

    s = build_statistics(factory, since=300.0, until=400.0).summary
    assert s.positions_opened == 0
    assert s.positions_closed == 1
    assert s.wins == 1


def test_until_is_exclusive(factory) -> None:
    store = PortfolioStore(factory)
    h = _open(store, "m1", ts=50.0)
    store.close_position(h.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
    # closed_at == until -> excluded
    s = build_statistics(factory, since=100.0, until=200.0).summary
    assert s.positions_closed == 0


def test_since_is_inclusive(factory) -> None:
    store = PortfolioStore(factory)
    h = _open(store, "m1", ts=50.0)
    store.close_position(h.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
    # closed_at == since -> included
    s = build_statistics(factory, since=200.0, until=300.0).summary
    assert s.positions_closed == 1


def test_pnl_curve_cumulative_ordered_by_closed_at(factory) -> None:
    store = PortfolioStore(factory)
    h1 = _open(store, "m1", ts=50.0)
    h2 = _open(store, "m2", ts=60.0)
    # Close out of chronological insertion order: m2 first (loss), then m1 (win).
    store.close_position(h2.position_id, sell_price=0.30, ts=150.0, close_reason="stop_loss")  # -1.0
    store.close_position(h1.position_id, sell_price=0.60, ts=250.0, close_reason="take_profit")  # +2.0

    curve = build_statistics(factory).pnl_curve
    assert [p.ts for p in curve] == [150.0, 250.0]
    assert curve[0].cumulative_pnl == pytest.approx(-1.0)
    assert curve[1].cumulative_pnl == pytest.approx(1.0)


def test_closed_positions_capped_at_200_newest_first(factory) -> None:
    store = PortfolioStore(factory)
    for i in range(205):
        h = _open(store, f"m{i}", ts=float(i))
        store.close_position(h.position_id, sell_price=0.50, ts=float(1000 + i), close_reason="take_profit")

    result = build_statistics(factory)
    assert result.closed_positions_truncated is True
    assert len(result.closed_positions) == 200
    assert result.closed_positions[0].closed_at == pytest.approx(1204.0)  # newest
    assert result.closed_positions[-1].closed_at == pytest.approx(1005.0)  # 200th-newest


def test_average_hold_seconds(factory) -> None:
    store = PortfolioStore(factory)
    h1 = _open(store, "m1", ts=100.0)
    store.close_position(h1.position_id, sell_price=0.50, ts=200.0, close_reason="take_profit")  # 100s
    h2 = _open(store, "m2", ts=100.0)
    store.close_position(h2.position_id, sell_price=0.50, ts=400.0, close_reason="take_profit")  # 300s

    s = build_statistics(factory).summary
    assert s.average_hold_seconds == pytest.approx(200.0)


def test_close_reason_breakdown_counts(factory) -> None:
    store = PortfolioStore(factory)
    reasons = ["take_profit", "take_profit", "stop_loss", "manual"]
    for i, reason in enumerate(reasons):
        h = _open(store, f"m{i}", ts=100.0 + i)
        store.close_position(h.position_id, sell_price=0.50, ts=200.0 + i, close_reason=reason)

    s = build_statistics(factory).summary
    assert s.close_reason_breakdown == {"take_profit": 2, "stop_loss": 1, "manual": 1}
