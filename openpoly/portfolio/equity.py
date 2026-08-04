"""Equity-curve reconstruction — folds the position ledger against sampled
order books into a realized + unrealized P&L time series.

Pure read side: no state, reads ``position`` and ``order_book_snapshot``.
``build_equity_curve`` recomputes from scratch per call. Full-history mode
(``window_seconds=None``) was fine at openPoly's original grain-of-rice scale,
but ``order_book_snapshot`` grows unboundedly (a fresh sample roughly every
``book_sample_interval_seconds``, forever) — the API route now always passes
a bounded ``window_seconds`` so the per-request cost stays roughly constant
over the life of a long-running process instead of growing every day. A
position closed before the window is dropped from the curve entirely; its
realized P&L belongs in a separate all-time summary (see
``openpoly.api.portfolio_routes._all_time_equity_summary``), not here.
``window_seconds=None`` remains available for full-history reconstruction —
and is what the existing test suite exercises for byte-identical coverage of
the pre-windowing behavior.

Unrealized P&L marks each open position at the level-1 bid of its token's most
recent snapshot at-or-before the evaluated time (hold-last). That is the price
the exit executor would actually sell into — an honest "if I closed now" mark.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import OrderBookSnapshot, PositionRow


@dataclass(frozen=True)
class EquityPoint:
    """Wallet equity at one instant. ``equity == realized + unrealized``."""

    ts: float  # epoch seconds, UTC
    equity: float
    realized: float
    unrealized: float


@dataclass(frozen=True)
class EquityCurve:
    """The full curve plus a summary snapshot (the last point's values)."""

    points: tuple[EquityPoint, ...]  # ascending by ts, de-duplicated
    realized: float
    unrealized: float
    total: float
    open_positions: int


def _best_bid(bids_json: str) -> float | None:
    """First (best) bid price from a stored depth ladder, or None if empty."""
    bids = json.loads(bids_json)
    if not bids:
        return None
    return float(bids[0][0])


def _advance_mark(
    marks: list[tuple[float, float]], cursor: int, t: float
) -> tuple[float | None, int]:
    """Advance ``cursor`` (index of the last-consumed mark; ``-1`` means none
    consumed yet) forward through ``marks`` (ascending by ts) to the last
    entry at-or-before ``t``, returning ``(bid_or_None, new_cursor)`` — the
    same "most recent bid at-or-before t" result the old ``_mark_at`` linear
    rescan produced, just computed incrementally.

    Caller must invoke this with non-decreasing ``t`` per token across one
    pass over a sorted timeline — that monotonicity is what makes the total
    cost O(len(marks) + len(timeline)) instead of O(len(marks) *
    len(timeline)) (rescanning ``marks`` from the start on every call).
    """
    n = len(marks)
    while cursor + 1 < n and marks[cursor + 1][0] <= t:
        cursor += 1
    return (marks[cursor][1] if cursor >= 0 else None), cursor


def build_equity_curve(
    session_factory: sessionmaker[Session],
    *,
    window_seconds: float | None = None,
    now: float | None = None,
) -> EquityCurve:
    """Reconstruct the realized + unrealized equity time series.

    ``window_seconds``, when given, bounds the curve to positions still open
    or closed within the last ``window_seconds``. A single boundary-seed
    snapshot per token (the latest one strictly before the window) is still
    fetched so a position already open before the window starts marks
    correctly from the window's left edge, rather than reading as flat/zero
    until the first in-window sample arrives. ``now`` defaults to
    ``time.time()`` — exposed as a parameter for deterministic tests.
    """
    now = now if now is not None else time.time()
    since = now - window_seconds if window_seconds is not None else None

    with session_factory() as session:
        stmt = select(PositionRow)
        if since is not None:
            stmt = stmt.where(
                or_(PositionRow.closed_at.is_(None), PositionRow.closed_at >= since)
            )
        positions = list(session.execute(stmt).scalars().all())
        if not positions:
            return EquityCurve((), 0.0, 0.0, 0.0, 0)

        # Per-token bid series, ascending by recorded_at (empty books
        # dropped). When windowed, seeded with the single most-recent
        # pre-window snapshot so a position already open at `since` marks
        # correctly from the first axis point, not just once a fresh
        # in-window sample lands.
        marks: dict[str, list[tuple[float, float]]] = {}
        for token_id in {p.token_id for p in positions}:
            series: list[tuple[float, float]] = []
            if since is not None:
                seed = (
                    session.execute(
                        select(OrderBookSnapshot)
                        .where(
                            OrderBookSnapshot.token_id == token_id,
                            OrderBookSnapshot.recorded_at < since,
                        )
                        .order_by(OrderBookSnapshot.recorded_at.desc())
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                if seed is not None:
                    bid = _best_bid(seed.bids_json)
                    if bid is not None:
                        series.append((seed.recorded_at, bid))
            rows_stmt = select(OrderBookSnapshot).where(OrderBookSnapshot.token_id == token_id)
            if since is not None:
                rows_stmt = rows_stmt.where(OrderBookSnapshot.recorded_at >= since)
            rows_stmt = rows_stmt.order_by(OrderBookSnapshot.recorded_at)
            for r in session.execute(rows_stmt).scalars().all():
                bid = _best_bid(r.bids_json)
                if bid is not None:
                    series.append((r.recorded_at, bid))
            marks[token_id] = series

    # Time axis: every open/close event + every in-window snapshot + now
    # (+ `since`, for a clean left edge when windowed).
    axis: set[float] = {now}
    if since is not None:
        axis.add(since)
    for p in positions:
        axis.add(max(p.opened_at, since) if since is not None else p.opened_at)
        if p.closed_at is not None:
            axis.add(p.closed_at)
        end = p.closed_at if p.closed_at is not None else now
        for ts, _bid in marks.get(p.token_id, []):
            if since is not None and ts < since:
                continue  # the boundary-seed row feeds the mark cursor only
            if p.opened_at <= ts <= end:
                axis.add(ts)
    timeline = sorted(axis)

    cursors: dict[str, int] = dict.fromkeys(marks, -1)
    points: list[EquityPoint] = []

    # Sweep two sorted cursors alongside the timeline instead of rescanning
    # every position at every point. A position enters the active set when the
    # timeline passes its open and leaves at its close, where its realized P&L
    # accrues once into a running total.
    #
    # The naive walk was O(positions × timeline), and the timeline includes
    # every in-window order-book snapshot — at the default 60s sampling a
    # 30-day window is tens of thousands of points per token, re-walked for
    # every position, on every poll of /api/portfolio/equity. This is
    # O(P log P + T × currently-open), and only a handful are ever open at once.
    # Same incremental-cursor trick ``_advance_mark`` already uses for marks.
    by_open = sorted(positions, key=lambda p: p.opened_at)
    by_close = sorted(
        (p for p in positions if p.closed_at is not None),
        key=lambda p: p.closed_at,  # type: ignore[arg-type,return-value]
    )
    next_open = 0
    next_close = 0
    active: dict[int, PositionRow] = {}
    realized = 0.0

    for t in timeline:
        while next_open < len(by_open) and by_open[next_open].opened_at <= t:
            entering = by_open[next_open]
            active[entering.id] = entering
            next_open += 1
        # Close after open, so a position that opened and closed at the same
        # instant is never counted as active — matching the original
        # ``closed_at > t`` guard.
        while next_close < len(by_close) and by_close[next_close].closed_at <= t:  # type: ignore[operator]
            leaving = by_close[next_close]
            active.pop(leaving.id, None)
            realized += leaving.realized_pnl or 0.0
            next_close += 1

        unrealized = 0.0
        for p in active.values():
            mark, cursors[p.token_id] = _advance_mark(
                marks.get(p.token_id, []), cursors[p.token_id], t
            )
            if mark is None:
                mark = p.avg_entry_price
            unrealized += (mark - p.avg_entry_price) * p.qty
        points.append(
            EquityPoint(
                ts=t, equity=realized + unrealized, realized=realized, unrealized=unrealized
            )
        )

    last = points[-1]
    return EquityCurve(
        points=tuple(points),
        realized=last.realized,
        unrealized=last.unrealized,
        total=last.equity,
        open_positions=sum(1 for p in positions if p.status == "open"),
    )
