"""Shared best-effort market-identity lookup by on-chain ``condition_id``.

``PositionRecord``/statistics rows store ``condition_id``, not ``market_id``,
so resolving a position back to its market for display (``market_question``,
a Polymarket link) means an O(n) walk of the live catalog. That walk alone
leaves a market question blank the moment a market leaves live discovery
(resolved, expired, filtered out) — exactly the gap ``market_catalog``
(``openpoly.db.tables.MarketCatalogRow``) exists to close for the backtest
engine's own replay (see ``openpoly.backtest.historical_store``). This module
gives the live API routes (``portfolio_routes``, ``statistics_routes``,
``backtest_routes``) the same two-tier resolution, in one place, so the
fallback can't accidentally exist in one of them and not the others.

Only ``question`` and a Polymarket URL are backed by the persisted fallback —
that table doesn't carry volume/liquidity/tags/end_date, and reconstructing
those as zero/empty would read as "this market genuinely has none" rather
than "not known here." Callers that need those fields keep using
``MarketIdentity.market`` (``None`` on a DB-only fallback) exactly as they
did before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from openpoly.db.history_query import market_catalog_row_by_condition_id
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import Market, polymarket_url


@dataclass(frozen=True)
class MarketIdentity:
    question: str | None
    polymarket_url: str | None
    # The full live Market, only when the live catalog itself had a hit —
    # None on a DB-only fallback (or no hit at all). Callers that need
    # volume/liquidity/tags/end_date must treat those as unknown, not zero,
    # when this is None.
    market: Market | None


def lookup_market_identity(session: Session, condition_id: str) -> MarketIdentity:
    """Live catalog first (common case — the market is still discovered),
    falling back to the persisted ``market_catalog`` table for a market
    that has since left live discovery."""
    live = market_source_manager.store.get_by_condition(condition_id)
    if live is not None:
        return MarketIdentity(question=live.question, polymarket_url=polymarket_url(live), market=live)
    row = market_catalog_row_by_condition_id(session, condition_id)
    if row is None:
        return MarketIdentity(question=None, polymarket_url=None, market=None)
    url = f"https://polymarket.com/event/{row.slug}" if row.slug else None
    return MarketIdentity(question=row.question, polymarket_url=url, market=None)
