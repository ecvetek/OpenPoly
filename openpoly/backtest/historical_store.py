"""``HistoricalMarketStore`` — a read-only view over persisted order-book
history, used only during an offline backtest replay.

Subclasses the real ``MarketStore`` (not just duck-typed) and overrides only
``get`` / ``get_order_book`` — the two methods entry/exit sections (and
``PaperExecutor``) actually call. This matters for safety, not just economy
of code: ``engine.run_backtest`` swaps the live ``openpoly.markets.manager
.manager.store`` global for an instance of this class for the duration of a
replay (both ``edge_threshold_v0.py`` and ``threshold_v0.py`` document
reading that global directly, no capability injection). The live pipeline
being paused doesn't stop ``MarketSourceManager``'s own background discovery
/ book-sampling loops — those are a separate lifecycle the operator would
not expect a backtest to also require stopping (they're the live data feed
the rest of the app uses). So a poll can, in principle, race the swap window
and call ``.replace()`` / ``.union()`` / ``.set_order_books()`` on whatever
``manager.store`` currently is. Because those are inherited unmodified from
``MarketStore``, a racing call just mutates this throwaway instance's own
(otherwise-unused) internal dicts — inert, no crash, and no way to corrupt
the real catalog, which was already captured separately into the frozen
``markets`` snapshot below before the swap happened.

Market *metadata* (question, token ids, tradeable, condition_id) is not
persisted anywhere in this codebase — confirmed by reading
``openpoly/markets/store.py`` (pure in-memory) and ``openpoly/db/tables.py``
(no market/question/token table). So this class can only resolve markets
still present in the *live* catalog at the moment a backtest run starts — a
frozen snapshot of that catalog is passed in at construction. A historical
``market_id`` no longer in it is simply unresolvable; the caller
(``engine.py``) counts that, it does not silently drop it.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from openpoly.db.history_query import order_book_at_or_before
from openpoly.markets.models import Market, OrderBook
from openpoly.markets.store import MarketStore


class HistoricalMarketStore(MarketStore):
    def __init__(self, session: Session, markets: dict[str, Market]) -> None:
        super().__init__()
        self._session = session
        self._markets = markets
        # The replay clock — callers advance this (via set_clock) to the
        # timestamp of whatever historical event is being evaluated next, so
        # get_order_book resolves to "the book as of that moment", not "now".
        self._clock = 0.0

    def set_clock(self, ts: float) -> None:
        self._clock = ts

    def get(self, market_id: str) -> Market | None:
        return self._markets.get(market_id)

    def get_order_book(self, token_id: str) -> OrderBook | None:
        row = order_book_at_or_before(self._session, token_id, self._clock)
        if row is None:
            return None
        try:
            bids = [(float(p), float(s)) for p, s in json.loads(row.bids_json)]
            asks = [(float(p), float(s)) for p, s in json.loads(row.asks_json)]
        except (TypeError, ValueError, KeyError):
            return None
        return OrderBook(token_id=token_id, ts=row.recorded_at, bids=bids, asks=asks)
