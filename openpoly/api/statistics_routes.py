"""``GET /api/statistics`` — realized trading-performance summary over a
``[since, until)`` date range (read-only). See ``openpoly.portfolio.statistics``
for the aggregation; this file only handles query params and augments each
closed-position row with ``market_question`` (same pattern as
``portfolio_routes.list_positions``, minus ``analyzer_decisions``/
``unrealized_pnl`` — not needed for a compact trade-log row).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.engine import get_session_factory
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import Market
from openpoly.portfolio.statistics import build_statistics

router = APIRouter(prefix="/api", tags=["statistics"])


def _lookup_market(condition_id: str) -> Market | None:
    """Duplicated from ``portfolio_routes._lookup_market`` — that helper is
    module-private, so not imported across route modules; this one-liner is
    cheap enough to duplicate rather than promote to a shared module for a
    single extra caller."""
    return market_source_manager.store.get_by_condition(condition_id)


@router.get("/statistics")
def get_statistics(
    since: float | None = None,
    until: float | None = None,
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Win/loss ratio, P&L, and a closed-trades table for the given range.
    Both ``since``/``until`` omitted means all-time. No validation on
    ``since >= until`` — it deterministically yields an empty result set
    (matches this API's existing permissive-clamp philosophy; no combination
    the UI can produce actually triggers this)."""
    result = build_statistics(factory, since=since, until=until)
    closed_positions: list[dict[str, Any]] = []
    for record in result.closed_positions:
        body = asdict(record)
        market = _lookup_market(record.condition_id)
        body["market_question"] = market.question if market is not None else None
        closed_positions.append(body)
    return {
        "since": result.since,
        "until": result.until,
        "summary": asdict(result.summary),
        "pnl_curve": [asdict(p) for p in result.pnl_curve],
        "closed_positions": closed_positions,
        "closed_positions_truncated": result.closed_positions_truncated,
    }
