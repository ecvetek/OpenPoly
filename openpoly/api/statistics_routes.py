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
from openpoly.markets.catalog_lookup import lookup_market_identity
from openpoly.portfolio.statistics import build_statistics

router = APIRouter(prefix="/api", tags=["statistics"])


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
    # One session for the whole loop (not per-row) — market_catalog is only
    # ever consulted on a live-catalog miss, but even that occasional query
    # shouldn't pay for a fresh session on every closed-position row.
    with factory() as session:
        for record in result.closed_positions:
            body = asdict(record)
            body["market_question"] = lookup_market_identity(session, record.condition_id).question
            closed_positions.append(body)
    return {
        "since": result.since,
        "until": result.until,
        "summary": asdict(result.summary),
        "pnl_curve": [asdict(p) for p in result.pnl_curve],
        "closed_positions": closed_positions,
        "closed_positions_truncated": result.closed_positions_truncated,
    }
