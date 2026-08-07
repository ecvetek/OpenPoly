"""Tests for PortfolioStore — the synchronous fill-ledger + position store (PF2)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.portfolio import PortfolioStore


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


def _open(
    store: PortfolioStore,
    *,
    market_id: str = "m1",
    side: str = "yes",
    token_id: str = "ty1",
    condition_id: str = "0xc1",
    price: float = 0.42,
    qty: float = 20.0,
    ts: float = 100.0,
    news_id: str = "n1",
):
    return store.open_position(
        market_id=market_id,
        side=side,
        token_id=token_id,
        condition_id=condition_id,
        price=price,
        qty=qty,
        ts=ts,
        news_id=news_id,
    )


# ---------- open ----------


def test_open_writes_position_and_buy_fill(store) -> None:
    held = _open(store)
    assert held.market_id == "m1" and held.side == "yes"
    assert held.qty == 20.0 and held.avg_entry_price == 0.42
    assert held.token_id == "ty1" and held.condition_id == "0xc1"

    positions = store.list_positions()
    assert len(positions) == 1
    assert positions[0].status == "open"
    assert positions[0].realized_pnl is None

    fills = store.list_fills()
    assert len(fills) == 1
    assert fills[0].action == "buy"
    assert fills[0].price == 0.42
    assert fills[0].news_id == "n1"
    assert fills[0].position_id == held.position_id


# ---------- close ----------


def test_close_writes_sell_fill_and_closes(store) -> None:
    held = _open(store, price=0.40, qty=10.0)
    rec = store.close_position(
        held.position_id,
        sell_price=0.55,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    assert rec.status == "closed"
    assert rec.closed_at == 200.0
    assert rec.close_reason == "take_profit"
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 10.0)

    fills = store.list_fills()
    assert len(fills) == 2
    sell = next(f for f in fills if f.action == "sell")
    assert sell.price == 0.55
    assert sell.qty == 10.0
    assert sell.trigger == "take_profit"


def test_close_missing_position_raises(store) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.close_position(999, sell_price=0.5, ts=1.0, close_reason="manual")


def test_close_already_closed_raises(store) -> None:
    held = _open(store)
    store.close_position(held.position_id, sell_price=0.5, ts=2.0, close_reason="manual")
    with pytest.raises(ValueError, match="not open"):
        store.close_position(held.position_id, sell_price=0.5, ts=3.0, close_reason="manual")


# ---------- queries ----------


def test_get_open_positions(store) -> None:
    _open(store, market_id="m1", token_id="ty1")
    h2 = _open(store, market_id="m2", token_id="ty2", condition_id="0xc2")
    _open(store, market_id="m3", token_id="ty3", condition_id="0xc3")
    store.close_position(h2.position_id, sell_price=0.5, ts=300.0, close_reason="manual")
    assert {p.market_id for p in store.get_open_positions()} == {"m1", "m3"}


def test_get_open_position_by_market_side(store) -> None:
    _open(store, market_id="m1", side="yes", token_id="ty1")
    assert store.get_open_position("m1", "yes") is not None
    assert store.get_open_position("m1", "no") is None
    assert store.get_open_position("zzz", "yes") is None


# ---------- one-position invariant ----------


def test_second_open_same_market_side_rejected(store) -> None:
    _open(store, market_id="m1", side="yes", token_id="ty1")
    with pytest.raises(IntegrityError):
        _open(store, market_id="m1", side="yes", token_id="ty1")


def test_both_sides_of_a_market_allowed(store) -> None:
    _open(store, market_id="m1", side="yes", token_id="ty1")
    _open(store, market_id="m1", side="no", token_id="tn1")
    assert len(store.get_open_positions()) == 2


def test_reentry_after_close_allowed(store) -> None:
    h = _open(store, market_id="m1", side="yes", token_id="ty1")
    store.close_position(h.position_id, sell_price=0.5, ts=200.0, close_reason="manual")
    _open(store, market_id="m1", side="yes", token_id="ty1", ts=300.0)
    assert store.get_open_position("m1", "yes") is not None
    assert len(store.list_positions()) == 2


# ---------- ledger integrity ----------


def test_position_consistent_with_fill_ledger(store) -> None:
    """Every position is a faithful fold of its fills — the core guarantee that
    the position table is a projection, not a competing source of truth."""
    _open(store, market_id="m1", side="yes", token_id="ty1", price=0.42, qty=20.0)
    h2 = _open(
        store, market_id="m2", side="no", token_id="tn2", condition_id="0xc2", price=0.30, qty=10.0
    )
    store.close_position(h2.position_id, sell_price=0.45, ts=200.0, close_reason="stop_loss")

    fills_by_pos: dict[int, list] = {}
    for f in store.list_fills(limit=1000):
        fills_by_pos.setdefault(f.position_id, []).append(f)

    for pos in store.list_positions(limit=1000):
        fills = fills_by_pos[pos.id]
        buys = [f for f in fills if f.action == "buy"]
        sells = [f for f in fills if f.action == "sell"]
        assert len(buys) == 1
        buy = buys[0]
        assert pos.qty == buy.qty
        assert pos.avg_entry_price == buy.price
        if sells:
            assert len(sells) == 1
            assert pos.status == "closed"
            assert pos.realized_pnl == pytest.approx((sells[0].price - buy.price) * pos.qty)
        else:
            assert pos.status == "open"
            assert pos.realized_pnl is None

    assert {p.status for p in store.list_positions()} == {"open", "closed"}


# ---------- list ordering / limit ----------


def test_list_newest_first_and_limit(store) -> None:
    for i in range(5):
        _open(store, market_id=f"m{i}", token_id=f"ty{i}", condition_id=f"0xc{i}", ts=float(i))
    positions = store.list_positions(limit=3)
    assert len(positions) == 3
    assert positions[0].market_id == "m4"  # newest (highest id) first

    fills = store.list_fills(limit=2)
    assert len(fills) == 2
    assert fills[0].market_id == "m4"


# ---------- news_ids_for_positions (bulk) ----------


def test_news_ids_for_positions_bulk_lookup(store) -> None:
    """One query for many position_ids at once — same observable result as
    calling news_id_for_position per-id, just without the extra round-trips
    (the N+1 pattern this bulk method replaces at the list_positions route)."""
    h1 = _open(store, market_id="m1", condition_id="0xc1", news_id="n1")
    h2 = _open(store, market_id="m2", condition_id="0xc2", news_id="n2")
    h3 = _open(store, market_id="m3", condition_id="0xc3", news_id=None)

    result = store.news_ids_for_positions([h1.position_id, h2.position_id, h3.position_id])

    assert result == {h1.position_id: "n1", h2.position_id: "n2"}
    assert h3.position_id not in result
    # Sanity: matches what the single-lookup version returns per id.
    assert store.news_id_for_position(h1.position_id) == "n1"
    assert store.news_id_for_position(h3.position_id) is None


def test_news_ids_for_positions_empty_input_returns_empty_dict(store) -> None:
    assert store.news_ids_for_positions([]) == {}


def test_news_ids_for_positions_unknown_id_absent_from_result(store) -> None:
    h1 = _open(store, news_id="n1")
    result = store.news_ids_for_positions([h1.position_id, 999999])
    assert result == {h1.position_id: "n1"}


# ---------- total_realized_pnl (all-time summary, PF-perf) ----------


def test_total_realized_pnl_sums_closed_positions_only(store) -> None:
    h1 = _open(store, market_id="m1", condition_id="0xc1", price=0.40, qty=10.0)
    store.close_position(h1.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
    h2 = _open(store, market_id="m2", condition_id="0xc2", price=0.30, qty=20.0)
    store.close_position(h2.position_id, sell_price=0.25, ts=210.0, close_reason="stop_loss")
    _open(store, market_id="m3", condition_id="0xc3", price=0.50, qty=5.0)  # left open

    expected = (0.55 - 0.40) * 10.0 + (0.25 - 0.30) * 20.0
    assert store.total_realized_pnl() == pytest.approx(expected)


def test_total_realized_pnl_zero_when_no_closed_positions(store) -> None:
    assert store.total_realized_pnl() == 0.0
    _open(store)  # an open position must not contribute
    assert store.total_realized_pnl() == 0.0


# ---------- order_id / tx_hash (slice C) ----------


def test_open_position_persists_order_id_tx_hash(store) -> None:
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
        order_id="0xORDER",
        tx_hash="0xDEADBEEF",
    )
    fills = store.list_fills(limit=5)
    buy = next(f for f in fills if f.position_id == held.position_id and f.action == "buy")
    assert buy.order_id == "0xORDER"
    assert buy.tx_hash == "0xDEADBEEF"


def test_open_position_order_id_optional(store) -> None:
    """Existing paper open_position calls pass no order_id/tx_hash — defaults None."""
    held = store.open_position(
        market_id="m2",
        side="yes",
        token_id="t2",
        condition_id="0xm2",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n2",
    )
    fills = store.list_fills(limit=5)
    buy = next(f for f in fills if f.position_id == held.position_id and f.action == "buy")
    assert buy.order_id is None
    assert buy.tx_hash is None


def test_close_position_persists_order_id_tx_hash(store) -> None:
    held = store.open_position(
        market_id="m3",
        side="yes",
        token_id="t3",
        condition_id="0xm3",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n3",
    )
    store.close_position(
        held.position_id,
        sell_price=0.55,
        ts=200.0,
        close_reason="take_profit",
        order_id="0xSELL",
        tx_hash="0xCAFE",
    )
    fills = store.list_fills(limit=5)
    sell = next(f for f in fills if f.position_id == held.position_id and f.action == "sell")
    assert sell.order_id == "0xSELL"
    assert sell.tx_hash == "0xCAFE"


# ---------- record_sell (partial-fill aware) ----------


def test_record_sell_full_closes_position(store) -> None:
    """Selling the full open qty behaves like close_position."""
    held = _open(store, price=0.40, qty=10.0)
    rec = store.record_sell(
        held.position_id,
        sold_qty=10.0,
        sell_price=0.55,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    assert rec.status == "closed"
    assert rec.qty == 10.0
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 10.0)
    sell = next(f for f in store.list_fills() if f.action == "sell")
    assert sell.qty == 10.0


def test_record_sell_partial_keeps_open_with_reduced_qty(store) -> None:
    """A partial fill must reduce the open qty and leave the position OPEN —
    not mark the whole position closed (which strands the unsold remainder
    on-chain as an orphan)."""
    held = _open(store, price=0.40, qty=18.0)
    rec = store.record_sell(
        held.position_id,
        sold_qty=15.0,
        sell_price=0.55,
        ts=200.0,
        close_reason="stop_loss",
        trigger="stop_loss",
    )
    assert rec.status == "open"
    assert rec.qty == pytest.approx(3.0)  # 18 - 15 remaining
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 15.0)  # only sold qty
    fills = store.list_fills()
    sell = next(f for f in fills if f.action == "sell")
    assert sell.qty == pytest.approx(15.0)  # records ACTUAL filled qty


def test_record_sell_accrues_realized_across_partials(store) -> None:
    """Realized PnL accrues across successive partial sells; the position
    closes only when fully sold, with the summed realized."""
    held = _open(store, price=0.40, qty=18.0)
    store.record_sell(
        held.position_id,
        sold_qty=15.0,
        sell_price=0.55,
        ts=200.0,
        close_reason="stop_loss",
        trigger="stop_loss",
    )
    rec = store.record_sell(
        held.position_id,
        sold_qty=3.0,
        sell_price=0.60,
        ts=210.0,
        close_reason="stop_loss",
        trigger="stop_loss",
    )
    assert rec.status == "closed"
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 15.0 + (0.60 - 0.40) * 3.0)
    assert len([f for f in store.list_fills() if f.action == "sell"]) == 2


# ---------- news-confluence signals ----------


def test_record_signal_round_trip(store) -> None:
    held = _open(store)
    store.record_signal(
        held.position_id,
        news_id="n1",
        ts=100.0,
        side="yes",
        relation="opening",
        p_model=0.71,
        confidence="high",
    )
    store.record_signal(
        held.position_id,
        news_id="n2",
        ts=150.0,
        side="no",
        relation="contradict",
    )
    signals = store.signals_for_position(held.position_id)
    assert [s.relation for s in signals] == ["opening", "contradict"]  # oldest first
    assert (signals[0].news_id, signals[0].p_model, signals[0].confidence) == ("n1", 0.71, "high")
    assert (signals[1].side, signals[1].p_model, signals[1].confidence) == ("no", None, None)


def test_signals_for_position_is_empty_for_an_unknown_position(store) -> None:
    assert store.signals_for_position(9999) == []


def test_signals_for_positions_bulk(store) -> None:
    a = _open(store, market_id="m1")
    b = _open(store, market_id="m2")
    _open(store, market_id="m3")  # no signals at all
    store.record_signal(a.position_id, news_id="n1", ts=10.0, side="yes", relation="opening")
    store.record_signal(a.position_id, news_id="n2", ts=20.0, side="yes", relation="reinforce")
    store.record_signal(b.position_id, news_id="n3", ts=30.0, side="yes", relation="opening")

    out = store.signals_for_positions([a.position_id, b.position_id, 9999])
    assert sorted(out) == sorted([a.position_id, b.position_id])
    assert [s.news_id for s in out[a.position_id]] == ["n1", "n2"]
    assert len(out[b.position_id]) == 1


def test_signals_for_positions_empty_input_skips_the_query(store) -> None:
    assert store.signals_for_positions([]) == {}
