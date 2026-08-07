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

Market *metadata* (question, token ids, tradeable, condition_id) used to
live only in-memory (``MarketStore``), which made a historical
``market_id`` no longer in the live catalog unresolvable — a market that
resolved, expired, or fell out of a filter between the analyzer call and
the backtest run. ``MarketCatalogRow`` (see its own docstring) now persists
durable identity for every market a discovery poll or holding-sync fetch
has ever seen, independent of live discovery state. ``get()`` below checks
the frozen live snapshot first (fast, common case — most backtests replay
recent history where the market is still live), then falls back to that
persisted table. Only a market_id that was *never* captured by any poll —
e.g. malformed Gamma data that failed normalization — is still genuinely
unresolvable; the caller (``engine.py``) counts that case, it does not
silently drop it.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from openpoly.db.history_query import market_catalog_row, order_book_at_or_before
from openpoly.db.tables import MarketCatalogRow
from openpoly.markets.models import Market, OrderBook
from openpoly.markets.store import MarketStore


def _market_from_catalog_row(row: MarketCatalogRow) -> Market:
    """Reconstruct a ``Market`` from persisted identity alone. Fields the
    entry/exit sections never read (volume, liquidity, event metadata, ...)
    get inert filler values — only market_id/condition_id/token ids/
    neg_risk are functionally load-bearing for a replay.

    ``tradeable`` is always True: a market resolved from persisted-only
    identity was discovered (passed the live filter) at SOME point in the
    past — that's what made it AnalyzerCallRow-eligible in the first place.
    Its CURRENT live tradeable status has no bearing on whether the
    historical entry decision being replayed was valid.
    """
    return Market(
        market_id=row.market_id,
        condition_id=row.condition_id,
        question=row.question,
        slug=row.slug,
        yes_token_id=row.yes_token_id,
        no_token_id=row.no_token_id,
        end_date=None,
        best_bid=None,
        best_ask=None,
        spread=None,
        last_trade_price=None,
        volume_24h=0.0,
        liquidity=0.0,
        taker_fee_rate=None,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        event_id=None,
        event_title=None,
        event_tags=(),
        neg_risk=row.neg_risk,
        tradeable=True,
    )


class HistoricalMarketStore(MarketStore):
    def __init__(self, session: Session, markets: dict[str, Market]) -> None:
        super().__init__()
        self._session = session
        self._markets = markets
        # Per-run cache for the DB fallback below — a market_id can be
        # looked up once per analyzer-call row plus once per exit-replay
        # tick, and its persisted identity never changes mid-run. None is
        # cached too (a confirmed miss), so a genuinely-unresolvable
        # market_id doesn't re-query on every subsequent lookup.
        self._db_cache: dict[str, Market | None] = {}
        # The replay clock — callers advance this (via set_clock) to the
        # timestamp of whatever historical event is being evaluated next, so
        # get_order_book resolves to "the book as of that moment", not "now".
        self._clock = 0.0

    def set_clock(self, ts: float) -> None:
        self._clock = ts

    def get(self, market_id: str) -> Market | None:
        live = self._markets.get(market_id)
        if live is not None:
            return live
        if market_id in self._db_cache:
            return self._db_cache[market_id]
        row = market_catalog_row(self._session, market_id)
        resolved = _market_from_catalog_row(row) if row is not None else None
        self._db_cache[market_id] = resolved
        return resolved

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
