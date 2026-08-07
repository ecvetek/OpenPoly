"""Tests for BacktestPortfolio — parity with PortfolioStore.record_sell's
close-or-reduce semantics, since the whole point is that PaperExecutor and
the entry sections' gating logic run unmodified against this."""

from __future__ import annotations

import pytest

from openpoly.backtest.portfolio import BacktestPortfolio


def _open(pf: BacktestPortfolio, *, market_id="m1", side="yes", qty=20.0, price=0.40, ts=100.0):
    return pf.open_position(
        market_id=market_id,
        side=side,
        token_id=f"{side}-{market_id}",
        condition_id=f"0x{market_id}",
        price=price,
        qty=qty,
        ts=ts,
        news_id="n1",
    )


def test_open_position_assigns_negative_synthetic_ids() -> None:
    pf = BacktestPortfolio()
    a = _open(pf, market_id="m1")
    b = _open(pf, market_id="m2")
    assert a.position_id < 0
    assert b.position_id < 0
    assert a.position_id != b.position_id


def test_full_sell_closes_position_and_computes_realized_pnl() -> None:
    pf = BacktestPortfolio()
    held = _open(pf, qty=20.0, price=0.40)
    rec = pf.record_sell(
        held.position_id, sold_qty=20.0, sell_price=0.55, ts=200.0, close_reason="take_profit"
    )
    assert rec.status == "closed"
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 20.0)
    assert pf.get_open_position("m1", "yes") is None


def test_partial_sell_leaves_position_open_with_reduced_qty() -> None:
    pf = BacktestPortfolio()
    held = _open(pf, qty=20.0, price=0.40)
    rec = pf.record_sell(
        held.position_id, sold_qty=10.0, sell_price=0.65, ts=200.0, close_reason="scale_out"
    )
    assert rec.status == "open"
    assert rec.qty == pytest.approx(10.0)
    assert rec.realized_pnl == pytest.approx((0.65 - 0.40) * 10.0)

    still_open = pf.get_open_position("m1", "yes")
    assert still_open is not None
    assert still_open.qty == pytest.approx(10.0)


def test_second_partial_sell_accrues_realized_pnl_and_then_closes() -> None:
    pf = BacktestPortfolio()
    held = _open(pf, qty=20.0, price=0.40)
    pf.record_sell(
        held.position_id, sold_qty=10.0, sell_price=0.65, ts=200.0, close_reason="scale_out"
    )
    rec = pf.record_sell(
        held.position_id,
        sold_qty=10.0,
        sell_price=0.50,
        ts=300.0,
        close_reason="post_scale_out_stop",
    )
    assert rec.status == "closed"
    expected = (0.65 - 0.40) * 10.0 + (0.50 - 0.40) * 10.0
    assert rec.realized_pnl == pytest.approx(expected)


def test_sell_on_closed_position_raises() -> None:
    pf = BacktestPortfolio()
    held = _open(pf, qty=20.0, price=0.40)
    pf.record_sell(
        held.position_id, sold_qty=20.0, sell_price=0.55, ts=200.0, close_reason="take_profit"
    )
    with pytest.raises(ValueError):
        pf.record_sell(
            held.position_id, sold_qty=1.0, sell_price=0.55, ts=201.0, close_reason="manual"
        )


def test_get_open_positions_excludes_closed() -> None:
    pf = BacktestPortfolio()
    a = _open(pf, market_id="m1")
    _open(pf, market_id="m2")
    pf.record_sell(a.position_id, sold_qty=20.0, sell_price=0.5, ts=200.0, close_reason="manual")
    opens = pf.get_open_positions()
    assert len(opens) == 1
    assert opens[0].market_id == "m2"


def test_list_positions_includes_open_and_closed() -> None:
    pf = BacktestPortfolio()
    a = _open(pf, market_id="m1")
    _open(pf, market_id="m2")
    pf.record_sell(a.position_id, sold_qty=20.0, sell_price=0.5, ts=200.0, close_reason="manual")
    all_positions = pf.list_positions(limit=100)
    assert len(all_positions) == 2
    assert {p.market_id for p in all_positions} == {"m1", "m2"}


def test_all_closed_ascending_orders_by_closed_at() -> None:
    pf = BacktestPortfolio()
    a = _open(pf, market_id="m1", ts=100.0)
    b = _open(pf, market_id="m2", ts=100.0)
    # Close b first (earlier closed_at) even though it was opened after a's
    # sell would naturally come first if ordered by open — proves the sort
    # is by closed_at, not id or open order.
    pf.record_sell(b.position_id, sold_qty=20.0, sell_price=0.5, ts=150.0, close_reason="manual")
    pf.record_sell(a.position_id, sold_qty=20.0, sell_price=0.5, ts=250.0, close_reason="manual")
    closed = pf.all_closed_ascending()
    assert [c.market_id for c in closed] == ["m2", "m1"]


# ---------- news-confluence signals ----------


def test_record_and_read_signals() -> None:
    p = BacktestPortfolio()
    held = p.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="c1",
        price=0.40,
        qty=10.0,
        ts=100.0,
    )
    p.record_signal(
        held.position_id,
        news_id="n1",
        ts=100.0,
        side="yes",
        relation="opening",
        p_model=0.7,
        confidence="high",
    )
    p.record_signal(held.position_id, news_id="n2", ts=110.0, side="no", relation="contradict")

    signals = p.signals_for_position(held.position_id)
    assert [s.relation for s in signals] == ["opening", "contradict"]
    assert [s.news_id for s in signals] == ["n1", "n2"]
    assert signals[0].p_model == 0.7
    assert p.signals_for_position(-999) == []


def test_signals_for_positions_bulk_matches_the_store_contract() -> None:
    p = BacktestPortfolio()
    a = p.open_position(
        market_id="m1", side="yes", token_id="t1", condition_id="c1", price=0.4, qty=1.0, ts=1.0
    )
    b = p.open_position(
        market_id="m2", side="yes", token_id="t2", condition_id="c2", price=0.4, qty=1.0, ts=1.0
    )
    p.record_signal(a.position_id, news_id="n1", ts=1.0, side="yes", relation="opening")

    out = p.signals_for_positions([a.position_id, b.position_id])
    assert list(out) == [a.position_id]  # a position with no signals is absent
    assert len(out[a.position_id]) == 1
