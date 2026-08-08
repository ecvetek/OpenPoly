"""Portfolio endpoints — ``GET /api/positions``, ``GET /api/fills`` (read side)
and ``POST /api/positions/{id}/close`` (manual close).

The ``fill`` ledger is the source of truth; ``position`` is its materialized
projection. Reads are newest-first, bounded by ``limit``. The manual close
routes one open position through ``executor.execute_sell`` (close_reason
``manual``) — the same fill path the ExitMonitor uses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.engine import get_session_factory
from openpoly.db.tables import (
    AnalyzerCallRow,
    EntryDecisionRow,
    ExitDecisionRow,
    NewsItemRow,
)
from openpoly.execution import executor
from openpoly.markets.catalog_lookup import lookup_market_identity
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import Market, normalize_gamma_market, resolved_side
from openpoly.markets.polymarket_api import (
    fetch_market_by_id,
    fetch_markets_by_condition_id,
    fetch_price_history_range,
)
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.confluence import evaluate
from openpoly.portfolio.equity import build_equity_curve
from openpoly.portfolio.models import PositionRecord, PositionSignal
from openpoly.runtime import canvas_store
from openpoly.sections.exit.confluence_v0 import ConfluenceExitConfig

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
    ~1000 short-lived sessions at ``limit=500``. ``market_question`` /
    ``polymarket_url`` / ``market_end_date`` check the live in-memory catalog
    first, falling back to the persisted ``market_catalog`` table (one shared
    session for the whole loop, not one per row) for a market that's since
    left live discovery — ``market_end_date`` stays ``None`` on that
    fallback (not persisted); ``unrealized_pnl`` stays live-catalog-only.
    """
    rows = store.list_positions(_clamp(limit))
    news_by_position = store.news_ids_for_positions([r.id for r in rows])
    decisions_by_news = _lookup_analyzer_decisions_bulk(
        sorted(set(news_by_position.values())), factory
    )
    signals_by_position = store.signals_for_positions([r.id for r in rows])
    now = time.time()
    positions: list[dict[str, Any]] = []
    with factory() as session:
        for record in rows:
            body = asdict(record)
            identity = lookup_market_identity(session, record.condition_id)
            market = identity.market
            body["market_question"] = identity.question
            body["polymarket_url"] = identity.polymarket_url
            body["market_end_date"] = (
                market.end_date.timestamp() if market is not None and market.end_date else None
            )
            news_id = news_by_position.get(record.id)
            body["news_id"] = news_id
            body["analyzer_decisions"] = decisions_by_news.get(news_id, []) if news_id else []
            body["unrealized_pnl"] = _unrealized_pnl(record)
            signals = signals_by_position.get(record.id, [])
            body["confluence"] = _confluence_for(record, signals, now)
            body["news_signal_count"] = len(signals)
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

    - ``market_question`` / ``polymarket_url``: live catalog lookup by
      condition_id, falling back to the persisted ``market_catalog`` table
      for a market that's since left live discovery (filtered out or
      resolved) — ``None`` only if the market was never captured by any
      poll. UI falls back to displaying the condition_id / a plain
      (non-link) label.
    - ``market_end_date``: same live-catalog lookup, but with no persisted
      fallback (not a ``market_catalog`` column) — ``None`` whenever the
      market has left live discovery, even if ``market_question`` resolved.
      UI omits the expiry in that case.
    - ``news_id`` / ``news``: the news item that triggered this position.
      ``news_id`` is ``None`` for a paper/manual position with no news
      linkage. ``news`` (content/urgency/sentiment/published_at) is
      ``None`` when ``news_id`` is None, or that news row was never
      persisted.
    - ``analyzer_decisions``: list (newest-first) of every ``verdict=ok``
      analyzer call whose ``news_id`` matches this position's news_id.
      Each element carries rationale / self_check / p_model / confidence /
      ts. Empty list when ``news_id`` is None, or the analyzer never hit
      ``verdict=ok`` on it.
    - ``exit_decision``: the exit-monitor decision that actually closed
      this position (trigger/return_pct/peak_price/reason), or ``None``
      for an open position or one closed before persistence went live.
    - ``entry_decision``: the entry-section decision that opened this
      position — its raw ``signals`` dict at decision time (edge, spread,
      min_edge, max_spread, recent_move, ...) plus any ``reason`` — or
      ``None`` for a position that predates entry_decision persistence.
    - ``market_tags`` / ``market_volume_24h`` / ``market_liquidity`` /
      ``market_taker_fee_rate``: from the same catalog lookup as
      ``market_question``. All ``None``/``[]``-free (just ``None``) when the
      market is no longer catalogued.
    - ``unrealized_pnl``: "if I closed this right now" P&L for an **open**
      position, marked at the live level-1 bid (same convention the exit
      monitor uses to evaluate stop-loss/take-profit) — ``None`` while
      closed (use ``realized_pnl`` instead), or if there's no live order
      book for the token yet.
    - ``news_signals``: the position's news-confluence ledger, oldest first —
      every later analyzer decision on this market that was blocked by this
      position, plus the ``opening`` one. Each carries ``relation``
      (opening/reinforce/contradict), the side it wanted, p_model,
      confidence, the news content/urgency/sentiment when still persisted,
      and rationale/self_check from the matching ``verdict=ok`` analyzer
      call when one is found.
    - ``confluence``: ``{state, support, against}`` derived from those
      signals as of now, using the live exit section's TTL/count settings.
      ``state`` is what the confluence exit section keys its peak-drawdown
      threshold off; ``solo`` for any position opened before this shipped.
    """
    record = store.get_position(position_id)
    if record is None:
        raise HTTPException(status_code=404, detail="position not found")
    body = asdict(record)
    with factory() as session:
        identity = lookup_market_identity(session, record.condition_id)
    market = identity.market
    body["market_question"] = identity.question
    body["polymarket_url"] = identity.polymarket_url
    body["market_end_date"] = (
        market.end_date.timestamp() if market is not None and market.end_date else None
    )
    body["market_tags"] = list(market.event_tags) if market is not None else None
    body["market_volume_24h"] = market.volume_24h if market is not None else None
    body["market_liquidity"] = market.liquidity if market is not None else None
    body["market_taker_fee_rate"] = market.taker_fee_rate if market is not None else None
    # PositionRecord doesn't carry news_id (it lives on the BUY fill row).
    # Look it up via the store + then query the persisted analyzer_call table.
    news_id = store.news_id_for_position(position_id)
    body["news_id"] = news_id
    body["news"] = _lookup_news_summary(news_id, factory)
    body["analyzer_decisions"] = _lookup_analyzer_decisions(news_id, factory)
    body["exit_decision"] = _lookup_exit_decision(position_id, factory)
    body["entry_decision"] = _lookup_entry_decision(position_id, factory)
    body["unrealized_pnl"] = _unrealized_pnl(record)
    signals = store.signals_for_position(position_id)
    body["news_signals"] = _news_signals_body(signals, record.market_id, factory)
    body["confluence"] = _confluence_for(record, signals, time.time())
    return body


# How long a durable market lookup is trusted before re-fetching — bounds
# Gamma call volume under PositionDetail's ~3s poll without materially
# staling the expiry/resolution state it reports.
MARKET_LOOKUP_CACHE_TTL_SECONDS = 30

_market_lookup_cache: dict[str, tuple[float, Market | None]] = {}


async def _lookup_market_durable(market_id: str, condition_id: str) -> Market | None:
    """Resolve a position's market even after it has fallen out of the live
    discovery catalog (near-expiry filtered, or resolved).

    ``lookup_market_identity`` (``openpoly.markets.catalog_lookup``) only
    checks the in-memory catalog and the persisted ``market_catalog``
    identity table — sufficient for ``market_question``/``polymarket_url``,
    but this endpoint also needs ``market_end_date``/resolution/winning
    side, which aren't persisted there. This instead calls Gamma directly:

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


# Named window -> (window_seconds | None, fidelity_minutes). A None span
# means "all" — its fidelity is computed per-request from the position's
# actual elapsed lifetime (see _resolve_window) since there's no fixed span
# to size a constant fidelity against. Each entry is picked to land roughly
# 150-350 CLOB candles for that span — dense enough to look continuous,
# sparse enough to stay cheap.
PRICE_HISTORY_WINDOW_OPTIONS: dict[str, tuple[int | None, int]] = {
    "1h": (3_600, 1),  # ~60 pts @ 1-min candles
    "6h": (6 * 3_600, 2),  # ~180 pts @ 2-min candles
    "1d": (86_400, 5),  # ~288 pts @ 5-min candles
    "1w": (7 * 86_400, 30),  # ~336 pts @ 30-min candles
    "1m": (30 * 86_400, 120),  # ~360 pts @ 2-hour candles
    "all": (None, 0),
}
PRICE_HISTORY_WINDOW_DEFAULT = "all"
PRICE_HISTORY_TARGET_POINTS = 250
PRICE_HISTORY_MIN_FIDELITY_MINUTES = 1
PRICE_HISTORY_MAX_FIDELITY_MINUTES = 1_440  # 1-day candle ceiling

# CLOB's /prices-history hard-rejects any single (startTs, endTs) interval
# longer than this — undocumented anywhere, found by hitting the live
# endpoint directly (fails identically at every fidelity, including the
# coarsest 1440-min one, so it's a pure interval-length cap, not a
# too-many-candles limit). "1m"/"all" spans routinely exceed it, so they're
# served as multiple chunked requests, concatenated. CLOB_MAX_CHUNKS bounds
# worst-case latency/call-volume for a pathologically long-held position
# (20 chunks ~= 300 days) rather than making dozens of sequential requests.
CLOB_MAX_INTERVAL_SECONDS = 15 * 86_400
CLOB_MAX_CHUNKS = 20

# TTL differs by window: a live "1h" view should feel responsive to a fresh
# trade; an hours-wide "1m"/"all" candle can't visibly change within a few
# seconds, so it tolerates a much longer TTL — cutting CLOB call volume
# hardest exactly where responsiveness matters least. Load-bearing, not an
# optimization: once this line is always CLOB-sourced, every ~3s poll of
# PositionDetail would otherwise hit Polymarket's live API directly.
PRICE_HISTORY_CACHE_TTL_SECONDS: dict[str, int] = {
    "1h": 15,
    "6h": 20,
    "1d": 30,
    "1w": 60,
    "1m": 90,
    "all": 60,
}
PRICE_HISTORY_CACHE_TTL_DEFAULT_SECONDS = 60

# Keyed on (token_id, window, fidelity, until_bucket) — `until` advances every
# poll (tracks wall-clock "now" for an unresolved market), so it's bucketed to
# the window's own TTL; otherwise every poll would compute a distinct `until`
# and always miss. `since` doesn't need its own bucketing: it's deterministic
# from (window, until_bucket, opened_at).
_price_history_cache: dict[tuple[str, str, int, int], tuple[float, list[list[float]]]] = {}


def _resolve_window(window: str, opened_at: float, until: float) -> tuple[str, float, int]:
    """Resolve a (possibly-unrecognized) window key to (resolved_window,
    since, fidelity). Unrecognized values silently fall back to
    PRICE_HISTORY_WINDOW_DEFAULT — safelist convention, matches
    EQUITY_WINDOW_OPTIONS_HOURS. Named windows are the trailing
    ``window_seconds`` ending at ``until``, clipped so ``since`` never
    precedes ``opened_at``. ``"all"`` = ``opened_at`` through ``until``, with
    fidelity computed from the actual elapsed span to hit the same
    ~PRICE_HISTORY_TARGET_POINTS budget as the named windows.
    """
    resolved = window if window in PRICE_HISTORY_WINDOW_OPTIONS else PRICE_HISTORY_WINDOW_DEFAULT
    window_seconds, fidelity = PRICE_HISTORY_WINDOW_OPTIONS[resolved]
    if window_seconds is None:  # "all"
        elapsed = max(until - opened_at, 1.0)
        fidelity = max(
            PRICE_HISTORY_MIN_FIDELITY_MINUTES,
            min(PRICE_HISTORY_MAX_FIDELITY_MINUTES, round(elapsed / 60 / PRICE_HISTORY_TARGET_POINTS)),
        )
        return resolved, opened_at, fidelity
    return resolved, max(until - window_seconds, opened_at), fidelity


def _fetch_price_history_with_fallback(
    token_id: str, since: float, until: float, fidelity: int
) -> list[tuple[float, float]]:
    """One CLOB call with a defensive retry: fidelity below CLOB's documented
    default (10 min) is unverified as a hard floor, so a suspiciously-empty
    result at fine fidelity retries once at the known-good default rather
    than silently reporting no data. An empty result at fidelity>=10 is
    trusted as-is."""
    try:
        points = fetch_price_history_range(token_id, start_ts=since, end_ts=until, fidelity=fidelity)
    except Exception:  # noqa: BLE001 — best-effort; caller fails soft
        logger.warning(
            "CLOB price-history fetch failed for %s (fidelity=%d)", token_id, fidelity, exc_info=True
        )
        return []
    if points or fidelity >= 10:
        return points
    logger.warning(
        "CLOB returned 0 points at fidelity=%d for %s; retrying at fidelity=10", fidelity, token_id
    )
    try:
        return fetch_price_history_range(token_id, start_ts=since, end_ts=until, fidelity=10)
    except Exception:  # noqa: BLE001
        logger.warning("CLOB fallback fetch also failed for %s", token_id, exc_info=True)
        return []


def _fetch_price_history_chunked(
    token_id: str, since: float, until: float, fidelity: int
) -> list[tuple[float, float]]:
    """Split a (since, until) span exceeding CLOB_MAX_INTERVAL_SECONDS into
    consecutive chunks CLOB will actually accept, and concatenate the
    results — see CLOB_MAX_INTERVAL_SECONDS for why this is necessary."""
    if until - since > CLOB_MAX_CHUNKS * CLOB_MAX_INTERVAL_SECONDS:
        since = until - CLOB_MAX_CHUNKS * CLOB_MAX_INTERVAL_SECONDS
    points: list[tuple[float, float]] = []
    chunk_start = since
    while chunk_start < until:
        chunk_end = min(chunk_start + CLOB_MAX_INTERVAL_SECONDS, until)
        points.extend(_fetch_price_history_with_fallback(token_id, chunk_start, chunk_end, fidelity))
        chunk_start = chunk_end
    return points


async def _cached_price_history(
    token_id: str, window: str, since: float, until: float, fidelity: int, ttl: int
) -> list[list[float]]:
    now = time.time()
    until_bucket = int(until // ttl) * ttl
    key = (token_id, window, fidelity, until_bucket)
    cached = _price_history_cache.get(key)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]
    raw = await asyncio.to_thread(_fetch_price_history_chunked, token_id, since, until, fidelity)
    points = [[ts, price] for ts, price in raw]
    _price_history_cache[key] = (now, points)  # cache empty/failed results too
    return points


@router.get("/positions/{position_id}/price-history")
async def get_position_price_history(
    position_id: int,
    window: str = PRICE_HISTORY_WINDOW_DEFAULT,
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Price history for one position, spanning open through close and on to
    the market's expiry/resolution — not frozen at ``closed_at`` the way a
    raw ``/api/inspect/order-books/{token_id}`` window would be.

    The line is always CLOB-sourced (``fetch_price_history_range``), for the
    *entire* visible window, open portion included — this guarantees one
    consistent data source/density across a position's whole lifetime, so
    there's no visual seam at the close point the way splicing in local
    order-book snapshots (dense, ~5s cadence) with a CLOB backfill (sparse)
    used to produce. Local order-book snapshots are no longer read here; they
    remain the source for the exit monitor's peak tracking, the equity curve,
    backtest replay, and ``/api/inspect/order-books/{token_id}`` — this
    change is scoped to this one display endpoint.

    ``window`` selects both the visible span and its resolution (fidelity) —
    one of ``PRICE_HISTORY_WINDOW_OPTIONS``' keys (``1h``/``6h``/``1d``/
    ``1w``/``1m``/``all``); an unrecognized value silently falls back to
    ``PRICE_HISTORY_WINDOW_DEFAULT`` (``all``), matching this API's
    ``EQUITY_WINDOW_OPTIONS_HOURS`` safelist convention. Cached per
    (token_id, window, fidelity, time-bucket) — see
    ``PRICE_HISTORY_CACHE_TTL_SECONDS``.

    Response fields: ``window`` (the resolved key actually served — lets a
    caller detect the safelist fallback), ``price_history`` (``[ts, price]``
    pairs, oldest-first; empty on any CLOB failure — fails soft, never
    500s), ``market_end_date`` (epoch seconds, or None), ``market_resolved``
    (bool), ``winning_side`` (``"yes"`` / ``"no"`` / None).
    """
    record = store.get_position(position_id)
    if record is None:
        raise HTTPException(status_code=404, detail="position not found")

    market = await _lookup_market_durable(record.market_id, record.condition_id)
    market_end_ts = market.end_date.timestamp() if market is not None and market.end_date else None
    market_resolved = market is not None and market.closed and market.outcome_prices is not None
    winning_side = resolved_side(market.outcome_prices) if market is not None else None

    now = time.time()
    until = min(now, market_end_ts) if market_end_ts is not None else now
    resolved_window, since, fidelity = _resolve_window(window, record.opened_at, until)
    ttl = PRICE_HISTORY_CACHE_TTL_SECONDS.get(resolved_window, PRICE_HISTORY_CACHE_TTL_DEFAULT_SECONDS)
    price_history = await _cached_price_history(record.token_id, resolved_window, since, until, fidelity, ttl)

    return {
        "position_id": position_id,
        "token_id": record.token_id,
        "window": resolved_window,
        "price_history": price_history,
        "market_end_date": market_end_ts,
        "market_resolved": market_resolved,
        "winning_side": winning_side,
    }


def _lookup_analyzer_decisions(
    news_id: str | None, factory: sessionmaker[Session]
) -> list[dict[str, Any]]:
    """All ``verdict=ok`` analyzer calls whose news_id matches, newest first.

    Queries the persisted ``analyzer_call`` table (durable — survives a
    restart and the in-memory ring's ~200-entry eviction). Returns empty list
    when:
    - ``news_id`` is None (paper / manual position with no news linkage)
    - The analyzer hit only errored or skipped on this news_id

    Returned dicts are flattened to UI-friendly shape: rationale, self_check,
    p_model, confidence, ts (no internal AnalyzerCall fields like
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
            "self_check": r.self_check,
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
                "self_check": r.self_check,
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


def _lookup_entry_decision(
    position_id: int, factory: sessionmaker[Session]
) -> dict[str, Any] | None:
    """The entry-section decision that opened this position — its raw
    ``signals`` dict (edge/spread/min_edge/max_spread/recent_move/...) at
    decision time, plus any ``reason`` the section attached. ``None`` for a
    position that predates entry_decision persistence."""
    with factory() as session:
        row = (
            session.execute(
                select(EntryDecisionRow)
                .where(
                    EntryDecisionRow.position_id == position_id,
                    EntryDecisionRow.verdict == "ok",
                )
                .order_by(EntryDecisionRow.ts.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
    if row is None:
        return None
    signals: dict[str, Any] | None = None
    if row.signals_json:
        try:
            parsed = json.loads(row.signals_json)
            if isinstance(parsed, dict):
                signals = parsed
        except json.JSONDecodeError:
            signals = None
    return {"signals": signals, "reason": row.reason, "ts": row.ts}


def _confluence_params() -> tuple[float, int, str]:
    """``(support_ttl_seconds, reinforce_min_count, min_confidence)`` as the
    live exit section is configured on the canvas.

    Read best-effort and clamped to ``ConfluenceExitConfig``'s own defaults on
    anything unexpected — a canvas running a non-confluence exit impl has none
    of these keys, and a hand-edited one may have garbage. Mirrors
    ``orchestrator._canvas_config``'s rule that a stale or broken canvas must
    never break a read path.

    The read API is the ONLY place that has to reach for these: the exit
    section derives the state from its own config at tick time. Here they exist
    purely so the UI badge agrees with what the running strategy is doing.
    """
    defaults = ConfluenceExitConfig()
    ttl_min: Any = defaults.support_ttl_minutes
    min_count: Any = defaults.reinforce_min_count
    min_conf: Any = defaults.min_signal_confidence
    try:
        cfg = canvas_store.section_config("exit")
        ttl_min = cfg.get("support_ttl_minutes", ttl_min)
        min_count = cfg.get("reinforce_min_count", min_count)
        min_conf = cfg.get("min_signal_confidence", min_conf)
    except Exception:  # noqa: BLE001 — a broken canvas must not break this read
        logger.warning("confluence params: canvas read failed; using defaults", exc_info=True)
    if not isinstance(ttl_min, (int, float)) or ttl_min < 0:
        ttl_min = defaults.support_ttl_minutes
    if not isinstance(min_count, int) or min_count < 2:
        min_count = defaults.reinforce_min_count
    if min_conf not in ("low", "medium", "high"):
        min_conf = defaults.min_signal_confidence
    return float(ttl_min) * 60.0, min_count, min_conf


def _confluence_for(
    record: PositionRecord, signals: list[PositionSignal], now: float
) -> dict[str, Any]:
    ttl_seconds, min_count, min_conf = _confluence_params()
    conf = evaluate(
        signals,
        position_side=record.side,
        now=now,
        support_ttl_seconds=ttl_seconds,
        reinforce_min_count=min_count,
        min_confidence=min_conf,
    )
    return {"state": conf.state, "support": conf.support, "against": conf.against}


def _news_signals_body(
    signals: list[PositionSignal], market_id: str, factory: sessionmaker[Session]
) -> list[dict[str, Any]]:
    """The position's news-confluence ledger, oldest first, each row joined to
    its news item's content/urgency/sentiment and (when available) the
    ``verdict=ok`` analyzer call that produced the decision.

    One query for every referenced news item, plus one bulk analyzer-call
    query scoped to this position's ``market_id`` (same grouping pattern as
    ``_lookup_analyzer_decisions_bulk``), rather than one query per signal. A
    signal whose news row was evicted (or never persisted — the news sink is
    write-behind and best-effort) still appears, with ``content``/``urgency``/
    ``sentiment`` ``None``; likewise ``rationale``/``self_check`` are ``None``
    when no matching analyzer call is found — the decision itself is the fact
    that matters here, the text is context.

    The analyzer-call join is scoped to ``(news_id, market_id)`` rather than
    ``news_id`` alone: ``AnalyzerCallRow.market_id`` is already on the row,
    and guarding against the same news_id having been analyzed against a
    different market (schema allows it, even if rare in practice) is free."""
    if not signals:
        return []
    news_ids = sorted({s.news_id for s in signals})
    with factory() as session:
        news_rows = (
            session.execute(select(NewsItemRow).where(NewsItemRow.news_id.in_(news_ids)))
            .scalars()
            .all()
        )
        analyzer_rows = (
            session.execute(
                select(AnalyzerCallRow)
                .where(
                    AnalyzerCallRow.news_id.in_(news_ids),
                    AnalyzerCallRow.verdict == "ok",
                    AnalyzerCallRow.market_id == market_id,
                )
                .order_by(AnalyzerCallRow.ts.desc())
            )
            .scalars()
            .all()
        )
    by_id = {r.news_id: r for r in news_rows}
    # Rows are newest-first; the first one seen per news_id wins.
    analyzer_by_news_id: dict[str, AnalyzerCallRow] = {}
    for r in analyzer_rows:
        analyzer_by_news_id.setdefault(r.news_id, r)
    out: list[dict[str, Any]] = []
    for sig in signals:
        row = by_id.get(sig.news_id)
        call = analyzer_by_news_id.get(sig.news_id)
        out.append(
            {
                "news_id": sig.news_id,
                "ts": sig.ts,
                "side": sig.side,
                "relation": sig.relation,
                "p_model": sig.p_model,
                "confidence": sig.confidence,
                "content": row.content if row is not None else None,
                "urgency": row.urgency if row is not None else None,
                "sentiment": row.sentiment if row is not None else None,
                "rationale": call.rationale if call is not None else None,
                "self_check": call.self_check if call is not None else None,
            }
        )
    return out


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
