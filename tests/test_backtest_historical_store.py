"""Tests for HistoricalMarketStore — the read-only, at-or-before order-book
view a backtest swaps in for the live MarketStore singleton."""

from __future__ import annotations

import json

import pytest

from openpoly.backtest.historical_store import HistoricalMarketStore
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.tables import OrderBookSnapshot
from openpoly.markets.models import normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary


def _market(market_id: str = "m1"):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Will X happen?",
        "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
    }
    m = normalize_gamma_market(raw, event={"id": "e1", "title": "E", "tags": []})
    assert m is not None
    return m


@pytest.fixture
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/hist.db")
    init_db(engine)
    return make_session_factory(engine)


def test_get_resolves_from_frozen_snapshot(sf) -> None:
    m = _market()
    with sf() as session:
        store = HistoricalMarketStore(session, {"m1": m})
        assert store.get("m1") is m
        assert store.get("missing") is None


def test_get_order_book_returns_snapshot_at_or_before_clock(sf) -> None:
    with sf() as session:
        session.add_all(
            [
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=100.0,
                    bids_json=json.dumps([[0.40, 50]]),
                    asks_json=json.dumps([[0.42, 50]]),
                ),
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=200.0,
                    bids_json=json.dumps([[0.55, 50]]),
                    asks_json=json.dumps([[0.57, 50]]),
                ),
            ]
        )
        session.commit()

    with sf() as session:
        store = HistoricalMarketStore(session, {})
        store.set_clock(150.0)
        book = store.get_order_book("t1")
        assert book is not None
        assert book.bids[0][0] == pytest.approx(0.40)  # the 100.0 snapshot, not 200.0

        store.set_clock(250.0)
        book2 = store.get_order_book("t1")
        assert book2.bids[0][0] == pytest.approx(0.55)


def test_get_order_book_none_before_any_snapshot(sf) -> None:
    with sf() as session:
        session.add(
            OrderBookSnapshot(
                token_id="t1",
                recorded_at=100.0,
                bids_json=json.dumps([[0.40, 50]]),
                asks_json=json.dumps([[0.42, 50]]),
            )
        )
        session.commit()

    with sf() as session:
        store = HistoricalMarketStore(session, {})
        store.set_clock(50.0)  # before the only snapshot
        assert store.get_order_book("t1") is None


def test_inherited_market_store_methods_are_safe_no_ops(sf) -> None:
    """A concurrent live discovery/book-sampling poll racing the swap window
    must not crash — HistoricalMarketStore subclasses MarketStore so an
    unexpected .replace()/.union()/.set_order_books() call just mutates this
    throwaway instance's own inert internal state, not the real catalog."""
    with sf() as session:
        store = HistoricalMarketStore(session, {"m1": _market()})
        store.replace([_market("m2")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        store.union([_market("m3")])
        store.set_order_books([])
        assert isinstance(store, MarketStore)
        # The frozen historical snapshot is untouched by any of the above —
        # .get() never reads self._catalog.
        assert store.get("m1") is not None
        assert store.get("m2") is None
