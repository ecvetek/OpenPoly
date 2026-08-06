"""Tests for Executor — the level-1 paper fill service (PF3).

The executor reads the live MarketStore singleton, so each test gets a fresh
catalog via the autouse fixture and a fresh PortfolioStore on a throwaway DB.
"""

from __future__ import annotations

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution import PaperExecutor as Executor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook, normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary
from openpoly.portfolio import PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent


@pytest.fixture(autouse=True)
def _isolate_market_store():
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield
    market_source_manager.store = saved


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


def _market(market_id: str = "m1", *, clob: str | None = None):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Q?",
        "clobTokenIds": clob or f'["yes-{market_id}", "no-{market_id}"]',
    }
    m = normalize_gamma_market(raw, event={"id": "e", "title": "E", "tags": []})
    assert m is not None
    return m


def _book(
    token_id: str,
    *,
    bid: float = 0.40,
    ask: float = 0.42,
    bid_size: float = 100.0,
    ask_size: float = 100.0,
) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(bid, bid_size)] if bid_size else [],
        asks=[(ask, ask_size)] if ask_size else [],
    )


def _populate(market, *books: OrderBook) -> None:
    s = market_source_manager.store
    s.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
    s.set_order_books(list(books))


def _intent(market_id: str = "m1", side: str = "yes", qty: float = 20.0) -> OrderIntent:
    return OrderIntent(market_id=market_id, side=side, price=0.42, qty=qty)


# ---------- execute_buy ----------


def test_buy_fills_at_level1_ask(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    r = Executor(store).execute_buy(_intent(qty=20.0), news_id="n1", ts=100.0)
    assert r.filled
    assert r.price == 0.42
    assert r.qty == 20.0
    assert r.position_id is not None
    assert store.get_open_position("m1", "yes") is not None


def test_buy_qty_capped_by_level1_depth(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42, ask_size=5.0))
    r = Executor(store).execute_buy(_intent(qty=100.0), news_id="n1", ts=1.0)
    assert r.filled
    assert r.qty == 5.0  # capped to the level-1 ask depth


def test_buy_dust_skip(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    # qty 1 @ 0.42 = $0.42 notional, below the $1 floor.
    r = Executor(store).execute_buy(_intent(qty=1.0), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "dust"


def test_buy_position_exists_skip(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    ex = Executor(store)
    first = ex.execute_buy(_intent(), news_id="n1", ts=1.0)
    assert first.filled
    r = ex.execute_buy(_intent(), news_id="n2", ts=2.0)
    assert not r.filled
    assert r.skip_reason == "position_exists"
    assert r.position_id == first.position_id


def test_buy_market_not_found_skip(store) -> None:
    r = Executor(store).execute_buy(_intent(market_id="zzz"), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "market_not_found"


def test_buy_no_order_book_skip(store) -> None:
    _populate(_market())  # market in catalog, no books sampled
    r = Executor(store).execute_buy(_intent(), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "no_order_book"


def test_buy_no_ask_liquidity_skip(store) -> None:
    _populate(_market(), _book("yes-m1", ask_size=0.0))  # bids only
    r = Executor(store).execute_buy(_intent(), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "no_ask_liquidity"


def test_buy_no_token_skip(store) -> None:
    # Market carries only the YES token; side=no has no token.
    _populate(_market(clob='["yes-m1"]'), _book("yes-m1", ask=0.42))
    r = Executor(store).execute_buy(_intent(side="no"), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "no_token"


def test_buy_no_side_reads_no_token_book(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42), _book("no-m1", ask=0.55))
    r = Executor(store).execute_buy(_intent(side="no", qty=20.0), news_id="n1", ts=1.0)
    assert r.filled
    assert r.price == 0.55  # the NO token's own book, not a flipped YES book
    held = store.get_open_position("m1", "no")
    assert held is not None and held.token_id == "no-m1"


# ---------- execute_sell ----------


def test_sell_fills_at_level1_bid(store) -> None:
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    r = ex.execute_sell(held, close_reason="take_profit", ts=200.0, trigger="take_profit")
    assert r.filled
    assert r.price == 0.40  # level-1 bid
    assert store.get_open_position("m1", "yes") is None  # now closed


def test_sell_capped_by_bid_depth_leaves_remainder_open(store) -> None:
    """Paper sells are capped by level-1 bid depth, symmetric with the buy
    path's ask-depth cap. Selling the whole position into a thin book
    regardless of size made paper overstate exit liquidity and produced results
    that couldn't be compared to a live run, where a GTC sell partially fills
    and the remainder stays open for the next exit tick."""
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=100.0))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    assert held.qty == 20.0

    # Book thins out to 6 shares before the exit fires.
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=6.0))
    r = ex.execute_sell(held, close_reason="take_profit", ts=200.0, trigger="take_profit")
    assert r.filled
    assert r.qty == 6.0
    assert r.price == 0.40

    still_open = store.get_open_position("m1", "yes")
    assert still_open is not None, "unsold remainder must stay open, not vanish"
    assert still_open.qty == pytest.approx(14.0)

    # Depth returns; the next tick closes the rest.
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=100.0))
    r2 = ex.execute_sell(still_open, close_reason="take_profit", ts=300.0)
    assert r2.filled
    assert r2.qty == pytest.approx(14.0)
    assert store.get_open_position("m1", "yes") is None


def test_sell_qty_none_sells_full_remaining_by_default(store) -> None:
    """Default (qty=None) preserves every existing full-close caller's
    behavior exactly — settlement, manual close, and the baseline exit
    section never pass qty at all."""
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=100.0))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    r = ex.execute_sell(held, close_reason="take_profit", ts=200.0, trigger="take_profit")
    assert r.filled
    assert r.qty == 20.0
    assert store.get_open_position("m1", "yes") is None


def test_sell_partial_qty_leaves_remainder_open(store) -> None:
    """A scale-out exit requests a partial qty — the remainder stays open
    with the reduced amount, same as a liquidity-constrained partial."""
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=100.0))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None

    r = ex.execute_sell(held, close_reason="scale_out", ts=200.0, trigger="scale_out", qty=10.0)
    assert r.filled
    assert r.qty == 10.0

    still_open = store.get_open_position("m1", "yes")
    assert still_open is not None
    assert still_open.qty == pytest.approx(10.0)


def test_sell_partial_qty_capped_by_position_qty(store) -> None:
    """Requesting more than the position actually holds is capped, not
    over-sold — a caller can never sell more than position.qty."""
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=100.0))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None

    r = ex.execute_sell(held, close_reason="manual", ts=200.0, qty=999.0)
    assert r.filled
    assert r.qty == 20.0
    assert store.get_open_position("m1", "yes") is None


def test_sell_skips_when_bid_depth_is_dust(store) -> None:
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=100.0))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None

    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42, bid_size=0.5))  # $0.20
    r = ex.execute_sell(held, close_reason="manual", ts=2.0)
    assert not r.filled
    assert r.skip_reason == "dust"
    assert store.get_open_position("m1", "yes") is not None


def test_sell_no_order_book_skip(store) -> None:
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    market_source_manager.store.set_order_books([])  # book gone
    r = ex.execute_sell(held, close_reason="manual", ts=2.0)
    assert not r.filled
    assert r.skip_reason == "no_order_book"


# ---------- configuration ----------


def test_unconfigured_executor_raises() -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    with pytest.raises(RuntimeError, match="PortfolioStore"):
        Executor().execute_buy(_intent(), news_id="n1", ts=1.0)
