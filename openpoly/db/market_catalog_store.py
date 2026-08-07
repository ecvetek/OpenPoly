"""Market-identity persistence — bridges the ``Market`` domain object to the
``market_catalog`` table, and builds the write-behind sink.

Unlike ``book_store.py`` (append-only — one row per sample), this is an
*upsert*: at most one row per ``market_id``, kept current as a market's
identity is re-observed on later polls. ``question``/token ids essentially
never change once a market exists, but the row is refreshed anyway (cheap,
and self-healing if Gamma ever corrects a typo) — only ``first_seen_at`` is
preserved across an update.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import MarketCatalogRow
from openpoly.db.writer import Sink
from openpoly.markets.models import Market


def upsert_market_catalog_row(session: Session, market: Market, *, now: float) -> None:
    """Insert or update the one row for ``market.market_id``. Caller owns the
    session/transaction (batched by the sink below, or called directly by a
    single-market write path)."""
    row = session.get(MarketCatalogRow, market.market_id)
    if row is None:
        session.add(
            MarketCatalogRow(
                market_id=market.market_id,
                condition_id=market.condition_id,
                question=market.question,
                slug=market.slug,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                neg_risk=market.neg_risk,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        return
    row.condition_id = market.condition_id
    row.question = market.question
    row.slug = market.slug
    row.yes_token_id = market.yes_token_id
    row.no_token_id = market.no_token_id
    row.neg_risk = market.neg_risk
    row.last_seen_at = now


def make_market_catalog_sink(session_factory: sessionmaker[Session]) -> Sink:
    """Build a write-behind sink that upserts a batch of ``Market``s.

    The returned callable is sync (the writer runs it in a worker thread, off
    the loop). One session per batch, same as ``make_order_book_sink``.
    """

    def sink(markets: list[Market]) -> None:
        if not markets:
            return
        now = time.time()
        with session_factory() as session:
            for market in markets:
                upsert_market_catalog_row(session, market, now=now)
            session.commit()

    return sink
