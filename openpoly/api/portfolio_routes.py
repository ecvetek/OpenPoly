"""Portfolio endpoints — ``GET /api/positions``, ``GET /api/fills`` (read side)
and ``POST /api/positions/{id}/close`` (manual close).

The ``fill`` ledger is the source of truth; ``position`` is its materialized
projection. Reads are newest-first, bounded by ``limit``. The manual close
routes one open position through ``executor.execute_sell`` (close_reason
``manual``) — the same fill path the ExitMonitor uses.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.engine import get_session_factory
from openpoly.db.tables import AnalyzerCallRow, ExitDecisionRow, NewsItemRow
from openpoly.execution import executor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import Market, polymarket_url
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.equity import build_equity_curve

router = APIRouter(prefix="/api", tags=["portfolio"])

LIMIT_DEFAULT = 100
LIMIT_MAX = 500


def get_portfolio_store() -> PortfolioStore:
    """Default dependency — a PortfolioStore on the process engine.
    Overridable via ``app.dependency_overrides`` in tests."""
    return PortfolioStore(get_session_factory())


def _clamp(limit: int) -> int:
    return max(1, min(limit, LIMIT_MAX))


@router.get("/positions")
def list_positions(
    limit: int = LIMIT_DEFAULT,
    store: PortfolioStore = Depends(get_portfolio_store),
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Recent positions (open + closed), newest first.

    Each row is augmented with ``market_question`` + ``analyzer_decisions``
    (same shape and fallback semantics as ``/positions/{id}`` — see that
    route's docstring). Card-style UI relies on these being available
    list-wide so it can render question / rationale without fanning out to
    /positions/{id} per row.
    """
    rows = store.list_positions(_clamp(limit))
    positions: list[dict[str, Any]] = []
    for record in rows:
        body = asdict(record)
        market = _lookup_market(record.condition_id)
        body["market_question"] = market.question if market is not None else None
        body["polymarket_url"] = polymarket_url(market)
        news_id = store.news_id_for_position(record.id)
        body["news_id"] = news_id
        body["analyzer_decisions"] = _lookup_analyzer_decisions(news_id, factory)
        positions.append(body)
    return {"positions": positions}


@router.get("/fills")
def list_fills(
    limit: int = LIMIT_DEFAULT,
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Recent fills — the ledger tail, newest first."""
    rows = store.list_fills(_clamp(limit))
    return {"fills": [asdict(f) for f in rows]}


@router.post("/positions/{position_id}/close")
async def close_position(
    position_id: int,
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Manually close one open position at the level-1 bid (close_reason
    ``manual``). 404 if no such position; 409 if it is already closed. The
    response body is the ``ExecResult`` — ``filled`` is False (with a
    ``skip_reason``) when the order book has no bid liquidity right now.

    Async, and it never awaits between the open-position lookup and the
    synchronous ``execute_sell`` — so the close is atomic with respect to the
    ExitMonitor tick on the same event loop (no double-close race).
    """
    held = next(
        (p for p in store.get_open_positions() if p.position_id == position_id),
        None,
    )
    if held is None:
        record = store.get_position(position_id)
        if record is None:
            raise HTTPException(status_code=404, detail="position not found")
        raise HTTPException(
            status_code=409,
            detail=f"position {position_id} is {record.status}, not open",
        )
    result = executor.execute_sell(held, close_reason="manual", ts=time.time(), trigger=None)
    return asdict(result)


@router.post("/positions/close-all")
async def close_all_positions(
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Bulk-close every currently-open position via the same level-1 bid path
    as the single-close route. Routes each ``execute_sell`` independently:
    one position's failure (e.g. ``no_bid_liquidity``) does not abort the
    others. Always returns 200 with a per-position result list — the caller
    decides what to do with the residuals.

    Same atomicity story as ``close_position``: the open snapshot is taken
    once at the top and each ``execute_sell`` is synchronous; no await
    interleaves between them and the ExitMonitor tick.
    """
    opens = store.get_open_positions()
    if not opens:
        return {"attempted": 0, "filled": 0, "skipped": 0, "errored": 0, "details": []}

    now = time.time()
    details: list[dict[str, Any]] = []
    filled = skipped = errored = 0
    for held in opens:
        entry: dict[str, Any] = {
            "position_id": held.position_id,
            "market_id": held.market_id,
            "side": held.side,
        }
        try:
            result = executor.execute_sell(held, close_reason="manual", ts=now, trigger=None)
        except Exception as exc:  # noqa: BLE001 — isolate per-position failure
            entry["ok"] = False
            entry["error"] = repr(exc)[:200]
            errored += 1
        else:
            if result.filled:
                entry["ok"] = True
                entry["price"] = result.price
                entry["qty"] = result.qty
                filled += 1
            else:
                entry["ok"] = False
                entry["skip_reason"] = result.skip_reason
                skipped += 1
        details.append(entry)
    return {
        "attempted": len(opens),
        "filled": filled,
        "skipped": skipped,
        "errored": errored,
        "details": details,
    }


@router.get("/positions/{position_id}")
def get_position_by_id(
    position_id: int,
    store: PortfolioStore = Depends(get_portfolio_store),
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """One position (open or closed) by id. 404 if no such position.

    Augments the raw PositionRecord with several best-effort lookups so the
    PositionDetail UI doesn't have to fan out to additional endpoints:

    - ``market_question`` / ``polymarket_url``: catalog lookup by
      condition_id. Both ``None`` when the market is no longer catalogued
      (filtered out or resolved). UI falls back to displaying the
      condition_id / a plain (non-link) label.
    - ``news_id`` / ``news``: the news item that triggered this position.
      ``news_id`` is ``None`` for a paper/manual position with no news
      linkage. ``news`` (content/urgency/sentiment/published_at) is
      ``None`` when ``news_id`` is None, or that news row was never
      persisted.
    - ``analyzer_decisions``: list (newest-first) of every ``verdict=ok``
      analyzer call whose ``news_id`` matches this position's news_id.
      Each element carries rationale / p_model / confidence / ts. Empty
      list when ``news_id`` is None, or the analyzer never hit
      ``verdict=ok`` on it.
    - ``exit_decision``: the exit-monitor decision that actually closed
      this position (trigger/return_pct/peak_price/reason), or ``None``
      for an open position or one closed before persistence went live.
    """
    record = store.get_position(position_id)
    if record is None:
        raise HTTPException(status_code=404, detail="position not found")
    body = asdict(record)
    market = _lookup_market(record.condition_id)
    body["market_question"] = market.question if market is not None else None
    body["polymarket_url"] = polymarket_url(market)
    # PositionRecord doesn't carry news_id (it lives on the BUY fill row).
    # Look it up via the store + then query the persisted analyzer_call table.
    news_id = store.news_id_for_position(position_id)
    body["news_id"] = news_id
    body["news"] = _lookup_news_summary(news_id, factory)
    body["analyzer_decisions"] = _lookup_analyzer_decisions(news_id, factory)
    body["exit_decision"] = _lookup_exit_decision(position_id, factory)
    return body


def _lookup_market(condition_id: str) -> Market | None:
    """Resolve PositionRecord.condition_id → Market via the live catalog.
    Best-effort: returns ``None`` when the market is no longer catalogued
    (filtered or resolved). Callers derive ``market_question`` /
    ``polymarket_url`` from this; frontend renders condition_id truncation
    as fallback."""
    return market_source_manager.store.get_by_condition(condition_id)


def _lookup_analyzer_decisions(
    news_id: str | None, factory: sessionmaker[Session]
) -> list[dict[str, Any]]:
    """All ``verdict=ok`` analyzer calls whose news_id matches, newest first.

    Queries the persisted ``analyzer_call`` table (durable — survives a
    restart and the in-memory ring's ~200-entry eviction). Returns empty list
    when:
    - ``news_id`` is None (paper / manual position with no news linkage)
    - The analyzer hit only errored or skipped on this news_id

    Returned dicts are flattened to UI-friendly shape: rationale, p_model,
    confidence, ts (no internal AnalyzerCall fields like
    news_content_preview / latency_ms / urgency — those are noise on the
    PositionDetail panel)."""
    if news_id is None:
        return []
    with factory() as session:
        rows = (
            session.execute(
                select(AnalyzerCallRow)
                .where(AnalyzerCallRow.news_id == news_id, AnalyzerCallRow.verdict == "ok")
                .order_by(AnalyzerCallRow.ts.desc())
            )
            .scalars()
            .all()
        )
    return [
        {
            "rationale": r.rationale,
            "p_model": r.p_model,
            "confidence": r.confidence,
            "ts": r.ts,
        }
        for r in rows
    ]


def _lookup_news_summary(
    news_id: str | None, factory: sessionmaker[Session]
) -> dict[str, Any] | None:
    """The triggering news item's content/urgency/sentiment/published_at, or
    ``None`` when ``news_id`` is None or that item was never persisted (the
    write-behind news sink is best-effort, same eviction story as any other
    persisted call-log row)."""
    if news_id is None:
        return None
    with factory() as session:
        row = (
            session.execute(select(NewsItemRow).where(NewsItemRow.news_id == news_id))
            .scalars()
            .first()
        )
    if row is None:
        return None
    return {
        "content": row.content,
        "urgency": row.urgency,
        "sentiment": row.sentiment,
        "published_at": row.published_at,
    }


def _lookup_exit_decision(
    position_id: int, factory: sessionmaker[Session]
) -> dict[str, Any] | None:
    """The exit-monitor decision that actually closed this position
    (verdict=ok, newest first) — ``None`` for a still-open position, or one
    closed before persistence went live."""
    with factory() as session:
        row = (
            session.execute(
                select(ExitDecisionRow)
                .where(
                    ExitDecisionRow.position_id == position_id,
                    ExitDecisionRow.verdict == "ok",
                )
                .order_by(ExitDecisionRow.ts.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
    if row is None:
        return None
    return {
        "trigger": row.trigger,
        "return_pct": row.return_pct,
        "fill_price": row.fill_price,
        "realized_pnl": row.realized_pnl,
        "reason": row.reason,
        "peak_price": row.peak_price,
        "ts": row.ts,
    }


@router.get("/portfolio/equity")
def get_equity_curve(
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Equity curve — realized + unrealized P&L over time, marked at the
    level-1 bid. Reconstructed per request from the position ledger + sampled
    order books; see ``openpoly.portfolio.equity``."""
    curve = build_equity_curve(factory)
    return {
        "points": [asdict(p) for p in curve.points],
        "summary": {
            "realized": curve.realized,
            "unrealized": curve.unrealized,
            "total": curve.total,
            "open_positions": curve.open_positions,
        },
    }
