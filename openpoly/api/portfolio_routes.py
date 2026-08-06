"""Portfolio endpoints — ``GET /api/positions``, ``GET /api/fills`` (read side)
and ``POST /api/positions/{id}/close`` (manual close).

The ``fill`` ledger is the source of truth; ``position`` is its materialized
projection. Reads are newest-first, bounded by ``limit``. The manual close
routes one open position through ``executor.execute_sell`` (close_reason
``manual``) — the same fill path the ExitMonitor uses.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.engine import get_session_factory
from openpoly.db.tables import AnalyzerCallRow, ExitDecisionRow, NewsItemRow, OrderBookSnapshot
from openpoly.execution import executor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import Market, normalize_gamma_market, polymarket_url, resolved_side
from openpoly.markets.polymarket_api import (
    fetch_market_by_id,
    fetch_markets_by_condition_id,
    fetch_price_history_range,
)
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.equity import build_equity_curve
from openpoly.portfolio.models import PositionRecord

logger = logging.getLogger(__name__)

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
    + ``unrealized_pnl`` (same shape and fallback semantics as
    ``/positions/{id}`` — see that route's docstring). Card-style UI relies
    on these being available list-wide so it can render question / rationale
    / live P&L without fanning out to /positions/{id} per row.

    ``news_id`` and ``analyzer_decisions`` are resolved via two bulk queries
    up front (not per-row) — this used to be up to 2 extra DB sessions per
    row (``news_id_for_position`` + a per-position analyzer_call lookup),
    ~1000 short-lived sessions at ``limit=500``. market_question/
    polymarket_url/unrealized_pnl stay per-row since those only touch the
    live in-memory catalog, not the DB.
    """
    rows = store.list_positions(_clamp(limit))
    news_by_position = store.news_ids_for_positions([r.id for r in rows])
    decisions_by_news = _lookup_analyzer_decisions_bulk(
        sorted(set(news_by_position.values())), factory
    )
    positions: list[dict[str, Any]] = []
    for record in rows:
        body = asdict(record)
        market = _lookup_market(record.condition_id)
        body["market_question"] = market.question if market is not None else None
        body["polymarket_url"] = polymarket_url(market)
        body["market_end_date"] = (
            market.end_date.timestamp() if market is not None and market.end_date else None
        )
        news_id = news_by_position.get(record.id)
        body["news_id"] = news_id
        body["analyzer_decisions"] = decisions_by_news.get(news_id, []) if news_id else []
        body["unrealized_pnl"] = _unrealized_pnl(record)
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

    - ``market_question`` / ``polymarket_url`` / ``market_end_date``: catalog
      lookup by condition_id. All ``None`` when the market is no longer
      catalogued (filtered out or resolved). UI falls back to displaying the
      condition_id / a plain (non-link) label, and omits the expiry.
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
    - ``unrealized_pnl``: "if I closed this right now" P&L for an **open**
      position, marked at the live level-1 bid (same convention the exit
      monitor uses to evaluate stop-loss/take-profit) — ``None`` while
      closed (use ``realized_pnl`` instead), or if there's no live order
      book for the token yet.
    """
    record = store.get_position(position_id)
    if record is None:
        raise HTTPException(status_code=404, detail="position not found")
    body = asdict(record)
    market = _lookup_market(record.condition_id)
    body["market_question"] = market.question if market is not None else None
    body["polymarket_url"] = polymarket_url(market)
    body["market_end_date"] = (
        market.end_date.timestamp() if market is not None and market.end_date else None
    )
    # PositionRecord doesn't carry news_id (it lives on the BUY fill row).
    # Look it up via the store + then query the persisted analyzer_call table.
    news_id = store.news_id_for_position(position_id)
    body["news_id"] = news_id
    body["news"] = _lookup_news_summary(news_id, factory)
    body["analyzer_decisions"] = _lookup_analyzer_decisions(news_id, factory)
    body["exit_decision"] = _lookup_exit_decision(position_id, factory)
    body["unrealized_pnl"] = _unrealized_pnl(record)
    return body


# Beyond this local-sampling gap, backfill from CLOB rather than trust the
# last local snapshot is "close enough" — roughly 3x the default
# ``book_sample_interval_seconds`` (60s), so ordinary sampling jitter never
# triggers an extra network call.
PRICE_HISTORY_GAP_THRESHOLD_SECONDS = 180
POSITION_PRICE_HISTORY_SNAPSHOT_LIMIT = 2000
# How long a durable market lookup is trusted before re-fetching — bounds
# Gamma call volume under PositionDetail's ~3s poll without materially
# staling the expiry/resolution state it reports.
MARKET_LOOKUP_CACHE_TTL_SECONDS = 30

_market_lookup_cache: dict[str, tuple[float, Market | None]] = {}


async def _lookup_market_durable(market_id: str, condition_id: str) -> Market | None:
    """Resolve a position's market even after it has fallen out of the live
    discovery catalog (near-expiry filtered, or resolved).

    ``_lookup_market`` (below) only sees markets currently in the in-memory
    catalog, which evicts a market well before expiry once a position closes
    on it. This instead calls Gamma directly:

    1. ``fetch_market_by_id`` (bypasses the ``/events`` top-100 window, same
       call the holding-sync hook uses to keep open positions catalogued) —
       covers a market that's still trading but fell out of discovery.
    2. If that comes back empty, or the market's ``end_date`` has already
       passed (resolution likely imminent or done), also tries
       ``fetch_markets_by_condition_id`` (``closed=true``) — the resolved-only
       path ``SettlementMonitor`` uses, since Gamma's id lookup mirrors
       ``/events``' open-only default and won't surface a resolved market.

    Cached briefly (``MARKET_LOOKUP_CACHE_TTL_SECONDS``) so a ~3s poll
    doesn't hammer Gamma on every tick.
    """
    now = time.time()
    cached = _market_lookup_cache.get(condition_id)
    if cached is not None and now - cached[0] < MARKET_LOOKUP_CACHE_TTL_SECONDS:
        return cached[1]

    market = await fetch_market_by_id(market_id)
    end_ts = market.end_date.timestamp() if market is not None and market.end_date else None
    if market is None or (end_ts is not None and end_ts < now):
        raw_markets = await fetch_markets_by_condition_id([condition_id])
        if raw_markets:
            resolved_market = normalize_gamma_market(raw_markets[0])
            if resolved_market is not None:
                market = resolved_market

    _market_lookup_cache[condition_id] = (now, market)
    return market


@router.get("/positions/{position_id}/price-history")
async def get_position_price_history(
    position_id: int,
    store: PortfolioStore = Depends(get_portfolio_store),
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Price history for one position, spanning open through close and on to
    the market's expiry/resolution — not frozen at ``closed_at`` the way a
    raw ``/api/inspect/order-books/{token_id}`` window would be.

    Local order-book sampling (``snapshots``: bid/ask bands + mid, same shape
    as the inspect route) stops once a market falls out of the live discovery
    catalog — for a closed position that's typically within one poll of
    close, well before expiry. Any gap between the last local snapshot and
    the window's upper bound is backfilled from Polymarket's own hosted CLOB
    price history (``price_points``: price only, no bands).

    Response fields: ``snapshots``, ``price_points`` (``[ts, price]`` pairs),
    ``market_end_date`` (epoch seconds, or None if the market can't be
    resolved at all), ``market_resolved`` (bool), ``winning_side``
    (``"yes"`` / ``"no"`` / None — None while unresolved or disputed).
    """
    record = store.get_position(position_id)
    if record is None:
        raise HTTPException(status_code=404, detail="position not found")

    market = await _lookup_market_durable(record.market_id, record.condition_id)
    market_end_ts = market.end_date.timestamp() if market is not None and market.end_date else None
    market_resolved = market is not None and market.closed and market.outcome_prices is not None
    winning_side = resolved_side(market.outcome_prices) if market is not None else None

    now = time.time()
    since = record.opened_at
    until = min(now, market_end_ts) if market_end_ts is not None else now

    with factory() as session:
        stmt = (
            select(OrderBookSnapshot)
            .where(
                OrderBookSnapshot.token_id == record.token_id,
                OrderBookSnapshot.recorded_at >= since,
                OrderBookSnapshot.recorded_at <= until,
            )
            .order_by(OrderBookSnapshot.recorded_at)
            .limit(POSITION_PRICE_HISTORY_SNAPSHOT_LIMIT)
        )
        rows = session.execute(stmt).scalars().all()

    snapshots = [
        {
            "recorded_at": r.recorded_at,
            "bids": json.loads(r.bids_json),
            "asks": json.loads(r.asks_json),
        }
        for r in rows
    ]

    last_local_ts = snapshots[-1]["recorded_at"] if snapshots else since
    price_points: list[tuple[float, float]] = []
    if until - last_local_ts > PRICE_HISTORY_GAP_THRESHOLD_SECONDS:
        try:
            price_points = fetch_price_history_range(
                record.token_id, start_ts=last_local_ts, end_ts=until
            )
        except Exception:  # noqa: BLE001 — best-effort backfill; local data still renders
            logger.warning(
                "CLOB price-history backfill failed for token %s", record.token_id, exc_info=True
            )

    return {
        "position_id": position_id,
        "token_id": record.token_id,
        "snapshots": snapshots,
        "price_points": [[ts, price] for ts, price in price_points],
        "market_end_date": market_end_ts,
        "market_resolved": market_resolved,
        "winning_side": winning_side,
    }


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


def _lookup_analyzer_decisions_bulk(
    news_ids: list[str], factory: sessionmaker[Session]
) -> dict[str, list[dict[str, Any]]]:
    """Bulk sibling of ``_lookup_analyzer_decisions`` — one query for many
    news_ids at once (grouped in Python), instead of one query per position
    row. Each group stays newest-first, same ordering contract as the
    single-news_id version."""
    if not news_ids:
        return {}
    with factory() as session:
        rows = (
            session.execute(
                select(AnalyzerCallRow)
                .where(AnalyzerCallRow.news_id.in_(news_ids), AnalyzerCallRow.verdict == "ok")
                .order_by(AnalyzerCallRow.ts.desc())
            )
            .scalars()
            .all()
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r.news_id, []).append(
            {
                "rationale": r.rationale,
                "p_model": r.p_model,
                "confidence": r.confidence,
                "ts": r.ts,
            }
        )
    return grouped


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


def _mark_unrealized(token_id: str, avg_entry_price: float, qty: float) -> float | None:
    """Mark-to-market core: live level-1-bid diff for one open position,
    keyed by its raw fields rather than a ``PositionRecord`` wrapper — lets
    both ``_unrealized_pnl`` (below) and ``_all_time_equity_summary`` (which
    works from ``HeldPosition``, a different dataclass with no ``status``
    field) share the same mark logic.

    ``None`` when there's no live order book yet for the token / it has no
    bids."""
    book = market_source_manager.store.get_order_book(token_id)
    if book is None or book.best_bid is None:
        return None
    return (book.best_bid - avg_entry_price) * qty


def _unrealized_pnl(record: PositionRecord) -> float | None:
    """Mark-to-market P&L for an open position — what closing it right now
    would realize, marked at the live level-1 bid. Same convention
    ``ExitMonitor._evaluate`` uses to decide stop-loss/take-profit
    (openpoly/runtime/exit_monitor.py), not ``build_equity_curve``'s
    persisted-snapshot hold-last mark (which is for historical
    reconstruction, not a live single-position read).

    ``None`` for a closed position (use ``realized_pnl`` instead), or when
    there's no live order book yet for the token / it has no bids."""
    if record.status != "open":
        return None
    return _mark_unrealized(record.token_id, record.avg_entry_price, record.qty)


# The equity chart is windowed to bound its cost as order_book_snapshot
# grows unboundedly over a long-running process's lifetime — see
# openpoly.portfolio.equity's module docstring. User-selectable (frontend:
# OverviewTab.tsx's WINDOW_OPTIONS); a query value outside this safelist
# falls back to the default rather than erroring, matching this API's
# existing permissive-clamp convention (SECTION_LOG_LIMIT_MAX etc.) — but a
# safelist rather than a plain min/max clamp, since an unbounded window
# would defeat the whole point of windowing. Keyed in hours (not days) so
# sub-day windows (1h/6h/12h) fit the same dict without going fractional.
EQUITY_WINDOW_OPTIONS_HOURS: dict[int, int] = {
    1: 3_600,
    6: 6 * 3_600,
    12: 12 * 3_600,
    24: 86_400,
    24 * 7: 7 * 86_400,
    24 * 30: 30 * 86_400,
}
EQUITY_WINDOW_DEFAULT_HOURS = 24


def _all_time_equity_summary(store: PortfolioStore) -> dict[str, Any]:
    """All-time realized + unrealized P&L — a cheap aggregate path,
    independent of the windowed equity curve. NOT derived from the curve's
    last point, which would silently drop any P&L that aged out of the
    window. Realized: one SUM query over every closed position
    (``PortfolioStore.total_realized_pnl``). Unrealized: live level-1-bid
    mark summed over every currently open position — cheap since there are
    only ever a handful open at once. A token with no live order book yet
    contributes 0.0, the same effective convention ``build_equity_curve``
    uses when no mark exists (falls back to entry price, i.e. zero diff)."""
    open_positions = store.get_open_positions()
    unrealized = sum(
        (_mark_unrealized(p.token_id, p.avg_entry_price, p.qty) or 0.0) for p in open_positions
    )
    realized = store.total_realized_pnl()
    return {
        "realized": realized,
        "unrealized": unrealized,
        "total": realized + unrealized,
        "open_positions": len(open_positions),
    }


@router.get("/portfolio/equity")
def get_equity_curve(
    window_hours: int = EQUITY_WINDOW_DEFAULT_HOURS,
    store: PortfolioStore = Depends(get_portfolio_store),
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Equity chart (``window_hours``, default 24) + all-time summary.

    ``points`` is windowed to bound the reconstruction cost as
    ``order_book_snapshot`` grows unboundedly over time — ``window_hours``
    must be one of ``EQUITY_WINDOW_OPTIONS_HOURS``' keys, anything else
    silently falls back to the default. ``summary`` is always all-time,
    computed via a separate cheap path — see ``_all_time_equity_summary``."""
    window_seconds = EQUITY_WINDOW_OPTIONS_HOURS.get(
        window_hours, EQUITY_WINDOW_OPTIONS_HOURS[EQUITY_WINDOW_DEFAULT_HOURS]
    )
    curve = build_equity_curve(factory, window_seconds=window_seconds)
    return {
        "points": [asdict(p) for p in curve.points],
        "summary": _all_time_equity_summary(store),
    }
