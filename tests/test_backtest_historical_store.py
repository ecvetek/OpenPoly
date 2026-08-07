"""Tests for HistoricalMarketStore — the read-only, at-or-before order-book
view a backtest swaps in for the live MarketStore singleton."""

from __future__ import annotations

import json

import pytest

from openpoly.backtest.historical_store import HistoricalMarketStore
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.market_catalog_store import upsert_market_catalog_row
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


# ---------- DB fallback for a market no longer in the live snapshot ----------


def test_get_falls_back_to_persisted_catalog_when_not_in_live_snapshot(sf) -> None:
    with sf() as session:
        upsert_market_catalog_row(session, _market("m1"), now=100.0)
        session.commit()

    with sf() as session:
        store = HistoricalMarketStore(session, {})  # empty live snapshot
        market = store.get("m1")
        assert market is not None
        assert market.market_id == "m1"
        assert market.condition_id == "0xm1"
        assert market.yes_token_id == "yes-m1"
        assert market.no_token_id == "no-m1"
        # Always tradeable for backtest purposes, regardless of the market's
        # current live status — see _market_from_catalog_row's docstring.
        assert market.tradeable is True


def test_get_prefers_live_snapshot_over_persisted_catalog(sf) -> None:
    live = _market("m1")
    with sf() as session:
        upsert_market_catalog_row(session, _market("m1"), now=100.0)
        session.commit()

    with sf() as session:
        store = HistoricalMarketStore(session, {"m1": live})
        assert store.get("m1") is live  # the live object, not a DB-reconstructed one


def test_get_returns_none_when_never_captured_by_any_poll(sf) -> None:
    with sf() as session:
        store = HistoricalMarketStore(session, {})
        assert store.get("never-seen") is None


def test_get_db_fallback_is_cached_across_calls(sf, monkeypatch) -> None:
    """A market_id looked up many times in one replay (once per analyzer
    call, once per exit tick) must hit the DB at most once."""
    with sf() as session:
        upsert_market_catalog_row(session, _market("m1"), now=100.0)
        session.commit()

    with sf() as session:
        store = HistoricalMarketStore(session, {})
        calls = {"n": 0}
        import openpoly.backtest.historical_store as hs_module

        original = hs_module.market_catalog_row

        def counting(session_arg, market_id):
            calls["n"] += 1
            return original(session_arg, market_id)

        monkeypatch.setattr(hs_module, "market_catalog_row", counting)

        store.get("m1")
        store.get("m1")
        store.get("m1")
        assert calls["n"] == 1


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
