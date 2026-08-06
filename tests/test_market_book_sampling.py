"""Tests for MS8 — order book sampling: MarketStore order-book storage +
MarketSourceManager's second loop. Both fetcher seams are injected (no network).
"""

from __future__ import annotations

import asyncio

from openpoly.markets.manager import MarketSourceConfig, MarketSourceManager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import MarketStore
from openpoly.portfolio.models import HeldPosition


def _book(token_id: str) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=[(0.4, 1.0)], asks=[(0.5, 1.0)])


def _held(position_id: int, token_id: str) -> HeldPosition:
    return HeldPosition(
        position_id=position_id,
        market_id=f"m{position_id}",
        side="yes",
        token_id=token_id,
        condition_id=f"0xm{position_id}",
        qty=1.0,
        avg_entry_price=0.5,
        opened_at=1.0,
    )


class _FakePortfolioStore:
    def __init__(self, positions: list[HeldPosition]) -> None:
        self._positions = positions

    def get_open_positions(self) -> list[HeldPosition]:
        return list(self._positions)


class _RaisingPortfolioStore:
    def get_open_positions(self) -> list[HeldPosition]:
        raise RuntimeError("db hiccup")


def _raw_pair(market_id: str):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Q?",
        "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
        "endDate": "2027-01-01T00:00:00Z",
        "bestBid": 0.40,
        "bestAsk": 0.42,
        "spread": 0.02,
        "volume24hr": 50_000.0,
        "liquidityNum": 20_000.0,
        "feesEnabled": False,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    return (raw, {"id": "e1", "title": "E", "tags": []})


def _fetcher(pairs):
    async def fetch(*, limit):
        return list(pairs)

    return fetch


async def _book_fetcher(token_id: str) -> OrderBook:
    return _book(token_id)


async def _wait_until(predicate, *, iterations: int = 600) -> None:
    for _ in range(iterations):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# ---------- MarketStore order-book storage ----------


def test_store_set_get_order_books():
    store = MarketStore()
    assert store.order_book_count == 0
    store.set_order_books([_book("a"), _book("b")])
    assert store.order_book_count == 2
    got = store.get_order_book("a")
    assert got is not None
    assert got.token_id == "a"


def test_store_set_order_books_replaces():
    store = MarketStore()
    store.set_order_books([_book("old1"), _book("old2")])
    store.set_order_books([_book("new1")])
    assert store.order_book_count == 1
    assert store.get_order_book("old1") is None
    assert store.get_order_book("new1") is not None


def test_store_get_order_book_missing():
    store = MarketStore()
    store.set_order_books([_book("a")])
    assert store.get_order_book("zzz") is None


def test_store_update_order_books_merges():
    store = MarketStore()
    store.set_order_books([_book("a"), _book("b")])
    store.update_order_books([_book("b"), _book("c")])
    assert store.order_book_count == 3
    assert store.get_order_book("a") is not None  # untouched by the merge
    assert store.get_order_book("b") is not None
    assert store.get_order_book("c") is not None


# ---------- MarketSourceConfig ----------


def test_config_book_interval_default():
    assert MarketSourceConfig().book_sample_interval_seconds == 60


def test_config_position_book_interval_default():
    assert MarketSourceConfig().position_book_interval_seconds == 5


# ---------- _sample_books_once ----------


async def test_sample_books_once():
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a"), _raw_pair("b")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()  # populate the catalog

    # Both sides of both markets are sampled.
    count = await mgr._sample_books_once()
    assert count == 4
    assert mgr.store.order_book_count == 4
    for token_id in ("yes-a", "no-a", "yes-b", "no-b"):
        assert mgr.store.get_order_book(token_id) is not None


async def test_sample_books_empty_catalog():
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    count = await mgr._sample_books_once()
    assert count == 0
    assert mgr.store.order_book_count == 0


async def test_sample_books_tolerates_fetch_failure():
    async def flaky(token_id: str) -> OrderBook:
        if token_id == "yes-b":
            raise RuntimeError("boom")
        return _book(token_id)

    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a"), _raw_pair("b"), _raw_pair("c")]),
        book_fetcher=flaky,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()

    # 3 markets x 2 sides = 6 fetches; only yes-b fails.
    count = await mgr._sample_books_once()
    assert count == 5
    assert mgr.store.get_order_book("yes-b") is None
    assert mgr.store.get_order_book("no-b") is not None  # NO side still kept
    assert mgr.store.get_order_book("yes-a") is not None
    assert mgr.store.get_order_book("yes-c") is not None


# ---------- _sample_position_books_once ----------


async def test_sample_position_books_no_portfolio_store():
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    count = await mgr._sample_position_books_once()
    assert count == 0
    assert mgr.store.order_book_count == 0


async def test_sample_position_books_no_open_positions():
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    mgr.set_portfolio_store(_FakePortfolioStore([]))
    count = await mgr._sample_position_books_once()
    assert count == 0


async def test_sample_position_books_tolerates_store_failure():
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    mgr.set_portfolio_store(_RaisingPortfolioStore())
    count = await mgr._sample_position_books_once()
    assert count == 0


async def test_sample_position_books_fetches_only_held_tokens_and_merges():
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a"), _raw_pair("b")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()
    await mgr._sample_books_once()  # full sweep populates all 4 tokens first
    assert mgr.store.order_book_count == 4

    mgr.set_portfolio_store(_FakePortfolioStore([_held(1, "yes-a")]))
    count = await mgr._sample_position_books_once()
    assert count == 1
    # merge, not replace — the other 3 tokens from the full sweep survive
    assert mgr.store.order_book_count == 4
    assert mgr.store.get_order_book("yes-a") is not None
    assert mgr.store.get_order_book("no-a") is not None
    assert mgr.store.get_order_book("yes-b") is not None


# ---------- book loop lifecycle ----------


async def test_book_loop_runs_on_start():
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a"), _raw_pair("b")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()  # pre-populate so the first book sample has markets

    await mgr.start(MarketSourceConfig(poll_interval_seconds=3600, book_sample_interval_seconds=5))
    await _wait_until(lambda: mgr.store.order_book_count >= 1)
    assert mgr.store.order_book_count == 4  # 2 markets x YES + NO
    assert any(e.kind == "book_sample_ok" for e in mgr.events())

    await mgr.stop()
    assert mgr.status().state == "stopped"


async def test_position_book_loop_runs_on_start():
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()
    mgr.set_portfolio_store(_FakePortfolioStore([_held(1, "yes-a")]))

    await mgr.start(
        MarketSourceConfig(
            poll_interval_seconds=3600,
            book_sample_interval_seconds=3600,
            position_book_interval_seconds=2,
        )
    )
    await _wait_until(lambda: any(e.kind == "position_book_sample_ok" for e in mgr.events()))
    assert mgr.store.get_order_book("yes-a") is not None

    await mgr.stop()
    assert mgr.status().state == "stopped"


async def test_stop_cancels_all_three_loops():
    mgr = MarketSourceManager(fetcher=_fetcher([_raw_pair("a")]), book_fetcher=_book_fetcher)
    await mgr.start(
        MarketSourceConfig(poll_interval_seconds=3600, book_sample_interval_seconds=3600)
    )
    await _wait_until(lambda: mgr.status().poll_count >= 1)
    snap = await mgr.stop()
    assert snap.state == "stopped"
    assert mgr._tasks == []
    # second stop is a safe no-op
    assert (await mgr.stop()).state == "stopped"
