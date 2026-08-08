"""``POST /api/backtest/run`` — replay a candidate entry+exit section config
against persisted history. See ``openpoly.backtest.engine`` for the actual
replay; this file only handles request/response shape and the
concurrent-backtest guard.

The live orchestrator and exit monitor have no pause control anywhere in
the API (see ``openpoly.backtest.guard``'s module docstring) — a backtest
does not wait for them to stop; instead it holds a guard flag for its
replay window that the live decision paths check and skip on. The only
thing this route needs to prevent is two backtests racing each other
(``run_backtest`` itself is not reentrant — the module-global store swap
can't be shared by two concurrent replays).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from openpoly.backtest.engine import BacktestRequest as ReplayRequest
from openpoly.backtest.engine import run_backtest
from openpoly.backtest.guard import BacktestAlreadyRunning, backtest_active
from openpoly.db.engine import get_session_factory
from openpoly.db.history_query import market_catalog_row_by_condition_id
from openpoly.markets.manager import manager as market_source_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


def _lookup_market_question(session: Session, condition_id: str) -> str | None:
    """Live catalog first (same pattern as statistics_routes.py's own
    duplicate of portfolio_routes._lookup_market — module-private, so not
    imported across route modules), falling back to the persisted
    market_catalog table. A backtest position can reference a market that's
    no longer live (that's the whole point of the persisted-catalog
    fallback in historical_store.py) — without this fallback here too, its
    question would silently go missing from the results table even though
    the replay itself resolved it correctly."""
    live = market_source_manager.store.get_by_condition(condition_id)
    if live is not None:
        return live.question
    row = market_catalog_row_by_condition_id(session, condition_id)
    return row.question if row is not None else None


# A backtest walks indexed point-queries over persisted history — cheap per
# row, but with no upper bound on row count for an unbounded range. 90 days
# is a generous backstop, not a measured limit (see engine.py's own module
# docstring on the sync-vs-async sizing assumption).
_MAX_RANGE_SECONDS = 90 * 86400


class SectionSelection(BaseModel):
    module: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


class BacktestRunRequest(BaseModel):
    since: float
    until: float
    entry: SectionSelection
    exit: SectionSelection


@router.post("/run")
def run(
    body: BacktestRunRequest,
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    if body.until <= body.since:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_range", "message": "until must be after since"},
        )
    if body.until - body.since > _MAX_RANGE_SECONDS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "range_too_large",
                "message": f"range exceeds the {_MAX_RANGE_SECONDS // 86400}-day cap",
            },
        )

    if backtest_active():
        raise HTTPException(
            status_code=409,
            detail={
                "error": "backtest_in_progress",
                "message": "another backtest is already running — try again shortly",
            },
        )

    try:
        result = run_backtest(
            ReplayRequest(
                since=body.since,
                until=body.until,
                entry_module=body.entry.module,
                entry_name=body.entry.name,
                entry_config=body.entry.config,
                exit_module=body.exit.module,
                exit_name=body.exit.name,
                exit_config=body.exit.config,
            ),
            factory,
        )
    except BacktestAlreadyRunning as exc:
        # The pre-check above is a fast path only — it isn't atomic with
        # run_backtest's own slot claim, so a genuine race between two
        # concurrent requests still has to be caught here to get the same
        # 409 (not the config-error 400 below).
        raise HTTPException(
            status_code=409,
            detail={
                "error": "backtest_in_progress",
                "message": "another backtest is already running — try again shortly",
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 — a bad impl/config choice must surface as 400, not a raw 500
        logger.exception("backtest run failed")
        raise HTTPException(
            status_code=400,
            detail={"error": "backtest_failed", "message": str(exc)[:500]},
        ) from exc

    stats = result.statistics
    closed_positions: list[dict[str, Any]] = []
    with factory() as session:
        for record in stats.closed_positions:
            row = asdict(record)
            row["market_question"] = _lookup_market_question(session, record.condition_id)
            closed_positions.append(row)
    return {
        "since": stats.since,
        "until": stats.until,
        "summary": asdict(stats.summary),
        "pnl_curve": [asdict(p) for p in stats.pnl_curve],
        "closed_positions": closed_positions,
        "closed_positions_truncated": stats.closed_positions_truncated,
        "replayed_analyzer_calls": result.replayed_analyzer_calls,
        "skipped_market_not_in_catalog": result.skipped_market_not_in_catalog,
        "elapsed_ms": result.elapsed_ms,
    }
