"""Statistics aggregation — realized trading-performance summary over a date
range (win/loss ratio, P&L, close-reason breakdown). Pure read side: reads
only ``position``. Mirrors ``equity.py``'s house style — a pure function over
a ``sessionmaker[Session]``, frozen dataclass result, ``since``/``until`` as
``float | None`` epoch-second bounds.

Unlike ``build_equity_curve`` (mark-to-market, needs ``now`` for the open
edge and touches ``order_book_snapshot``), this module never marks an open
position and never touches ``order_book_snapshot`` — ``since``/``until`` are
always caller-supplied explicitly (the frontend always sends both for
presets, or omits both for all-time), so there is no wall-clock dependency
here, no ``now`` parameter needed. This page is intentionally scoped to
realized, closed-trade performance; live/unrealized tracking is Overview's
job (``openpoly.api.portfolio_routes._all_time_equity_summary``).

``[since, until)`` is a HALF-OPEN interval — ``until`` is EXCLUSIVE. This is
a deliberate deviation from ``inspect_routes.inspect_order_book_history``'s
inclusive-both-ends convention: calendar-month bucketing needs a half-open
interval or the boundary instant double-counts into both months (a trade
closed at exactly local-midnight April 1 would otherwise satisfy both
March's ``<= until`` and April's ``>= since``). Applied uniformly to preset
ranges too, so there's one mental model rather than two.

Opened-vs-closed windowing choice (the same class of ambiguity ``equity.py``'s
own module docstring flags for its window): ``positions_opened`` counts by
``opened_at``; the win/loss/P&L summary, the P&L curve, and the closed-
positions table all scope by ``closed_at``, since that's when P&L is
realized — standard trading-journal convention (booked on realization date,
not initiation date). These two sets can overlap partially or not at all —
not a bug to reconcile.

Pulls matching rows and aggregates in Python rather than SQL ``SUM``/``GROUP
BY`` — the P&L curve and closed-positions table need row-level detail
regardless, so aggregating separately in SQL would mean querying the same
row set twice. Same approach ``equity.py`` already uses at this table. No
windowing safeguard needed for "all-time" (unlike ``equity.py``): ``position``
grows only with actual trades (one-shot per market/side), not an
unboundedly-growing sampling loop like ``order_book_snapshot``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import PositionRow
from openpoly.portfolio.models import PositionRecord

# Trades-table cap — this is a review page, not a paginated ledger; a range
# with more closed positions than this shows the newest 200 plus a "showing
# N of M" note rather than growing new pagination infra for it.
CLOSED_POSITIONS_TABLE_LIMIT = 200


@dataclass(frozen=True)
class PnlCurvePoint:
    """One point on the cumulative realized-P&L curve — the running sum of
    ``realized_pnl`` over closed-in-range positions, ordered by
    ``closed_at``."""

    ts: float  # epoch seconds, UTC — the closing position's closed_at
    cumulative_pnl: float


@dataclass(frozen=True)
class StatisticsSummary:
    """Aggregate metrics over the closed-in-range set, plus
    ``positions_opened`` (a separate ``opened_at``-scoped count — see module
    docstring)."""

    positions_opened: int
    positions_closed: int
    wins: int
    losses: int
    breakeven: int
    win_rate: (
        float | None
    )  # wins / (wins + losses); breakeven excluded from both terms. None if wins+losses == 0.
    gross_profit: float  # sum of positive realized_pnl — a magnitude, always >= 0.0
    gross_loss: float  # sum of abs(negative realized_pnl) — a magnitude, always >= 0.0 (needed for profit_factor; negate at display time)
    net_pnl: float  # gross_profit - gross_loss (signed)
    profit_factor: (
        float | None
    )  # gross_profit / gross_loss; None only if gross_loss == 0.0 (so all-losses -> 0.0, not None)
    average_win: float | None  # gross_profit / wins; None if wins == 0
    average_loss: (
        float | None
    )  # mean of the SIGNED losing realized_pnl values (<= 0.0); None if losses == 0
    largest_win: float | None  # max realized_pnl among wins; None if wins == 0
    largest_loss: (
        float | None
    )  # min realized_pnl among losses (most negative, signed); None if losses == 0
    average_hold_seconds: (
        float | None
    )  # mean(closed_at - opened_at) over positions_closed; None if positions_closed == 0
    close_reason_breakdown: dict[str, int]  # close_reason -> count, over positions_closed
    # Percent-return companions to the dollar figures above, each trade's
    # return computed as realized_pnl / (qty * avg_entry_price) — the same
    # cost-basis formula threshold_v0.py uses for its take-profit/stop-loss
    # triggers. A trade with zero cost basis (shouldn't happen in practice)
    # is excluded from every pct aggregate below, not treated as 0%.
    net_pnl_pct: (
        float | None
    )  # net_pnl / total cost basis of all closed-in-range rows; None if that total is 0
    average_win_pct: float | None  # mean of per-trade pct among wins; None if wins == 0
    average_loss_pct: (
        float | None
    )  # mean of per-trade pct among losses (signed, <= 0.0); None if losses == 0
    largest_win_pct: (
        float | None
    )  # pct of the SAME trade as largest_win (not independently maximized); None if wins == 0
    largest_loss_pct: (
        float | None
    )  # pct of the SAME trade as largest_loss (not independently maximized); None if losses == 0


@dataclass(frozen=True)
class StatisticsResult:
    since: float | None
    until: float | None
    summary: StatisticsSummary
    pnl_curve: tuple[PnlCurvePoint, ...]  # ascending by ts
    closed_positions: tuple[
        PositionRecord, ...
    ]  # newest-first, capped at CLOSED_POSITIONS_TABLE_LIMIT
    closed_positions_truncated: bool  # True when positions_closed > CLOSED_POSITIONS_TABLE_LIMIT


def _to_record(row: PositionRow) -> PositionRecord:
    """Byte-for-byte duplicate of ``store.py``'s module-private ``_to_record``
    — duplicated rather than imported to keep this module independent of
    ``store.py``'s internals, consistent with ``equity.py`` not depending on
    ``store.py`` either."""
    return PositionRecord(
        id=row.id,
        market_id=row.market_id,
        side=row.side,  # type: ignore[arg-type]
        token_id=row.token_id,
        condition_id=row.condition_id,
        qty=row.qty,
        avg_entry_price=row.avg_entry_price,
        status=row.status,  # type: ignore[arg-type]
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        close_reason=row.close_reason,
        realized_pnl=row.realized_pnl,
    )


def build_statistics(
    session_factory: sessionmaker[Session],
    *,
    since: float | None = None,
    until: float | None = None,
) -> StatisticsResult:
    """Realized trading-performance summary over ``[since, until)``. Both
    bounds ``None`` means all-time (no filtering) — safe here, unlike
    ``equity.py``'s windowing requirement, since ``position`` only grows with
    actual trades."""
    with session_factory() as session:
        opened_stmt = select(func.count()).select_from(PositionRow)
        if since is not None:
            opened_stmt = opened_stmt.where(PositionRow.opened_at >= since)
        if until is not None:
            opened_stmt = opened_stmt.where(PositionRow.opened_at < until)
        positions_opened = session.execute(opened_stmt).scalar_one()

        # status == "closed" (not closed_at.is_not(None)) mirrors
        # PortfolioStore.total_realized_pnl()'s existing filter idiom — the
        # two always transition together in store.py.
        closed_stmt = select(PositionRow).where(PositionRow.status == "closed")
        if since is not None:
            closed_stmt = closed_stmt.where(PositionRow.closed_at >= since)
        if until is not None:
            closed_stmt = closed_stmt.where(PositionRow.closed_at < until)
        closed_stmt = closed_stmt.order_by(PositionRow.closed_at.asc())
        closed_rows = list(session.execute(closed_stmt).scalars().all())

    wins = losses = breakeven = 0
    gross_profit = 0.0
    gross_loss = 0.0  # magnitude, >= 0.0
    win_pnls: list[float] = []
    loss_pnls: list[float] = []  # signed, <= 0.0
    # Index-aligned with win_pnls/loss_pnls (None where cost basis is 0, so
    # index i always refers to the same trade in both lists — needed to
    # pair largest_win/largest_loss with the matching pct below).
    win_pcts: list[float | None] = []
    loss_pcts: list[float | None] = []
    hold_seconds: list[float] = []
    close_reason_counts: dict[str, int] = {}
    cumulative = 0.0
    pnl_curve: list[PnlCurvePoint] = []
    total_cost_basis = 0.0

    for row in closed_rows:  # ascending by closed_at
        pnl = row.realized_pnl or 0.0
        cumulative += pnl
        pnl_curve.append(PnlCurvePoint(ts=row.closed_at, cumulative_pnl=cumulative))  # type: ignore[arg-type]
        cost = row.qty * row.avg_entry_price
        pct = (pnl / cost) if cost > 0 else None
        if cost > 0:
            total_cost_basis += cost
        if pnl > 0:
            wins += 1
            gross_profit += pnl
            win_pnls.append(pnl)
            win_pcts.append(pct)
        elif pnl < 0:
            losses += 1
            gross_loss += -pnl
            loss_pnls.append(pnl)
            loss_pcts.append(pct)
        else:
            breakeven += 1
        hold_seconds.append(row.closed_at - row.opened_at)  # type: ignore[operator]
        reason = row.close_reason or "unknown"
        close_reason_counts[reason] = close_reason_counts.get(reason, 0) + 1

    positions_closed = len(closed_rows)
    net_pnl = gross_profit - gross_loss
    # largest_win/largest_loss pct pairs with the SAME trade as the dollar
    # figure (index into the parallel *_pnls list), not an independently
    # maximized percent — a small trade with a huge % swing shouldn't be
    # reported as "largest" here when a different trade is the dollar
    # largest. None if that particular trade had no valid cost basis.
    largest_win_pct = win_pcts[win_pnls.index(max(win_pnls))] if win_pnls else None
    largest_loss_pct = loss_pcts[loss_pnls.index(min(loss_pnls))] if loss_pnls else None
    win_pcts_valid = [p for p in win_pcts if p is not None]
    loss_pcts_valid = [p for p in loss_pcts if p is not None]
    summary = StatisticsSummary(
        positions_opened=positions_opened,
        positions_closed=positions_closed,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        win_rate=(wins / (wins + losses)) if (wins + losses) > 0 else None,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_pnl=net_pnl,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        average_win=(gross_profit / wins) if wins > 0 else None,
        average_loss=(sum(loss_pnls) / losses) if losses > 0 else None,
        largest_win=max(win_pnls) if win_pnls else None,
        largest_loss=min(loss_pnls) if loss_pnls else None,
        average_hold_seconds=(sum(hold_seconds) / len(hold_seconds)) if hold_seconds else None,
        close_reason_breakdown=close_reason_counts,
        net_pnl_pct=(net_pnl / total_cost_basis) if total_cost_basis > 0 else None,
        average_win_pct=((sum(win_pcts_valid) / len(win_pcts_valid)) if win_pcts_valid else None),
        average_loss_pct=(
            (sum(loss_pcts_valid) / len(loss_pcts_valid)) if loss_pcts_valid else None
        ),
        largest_win_pct=largest_win_pct,
        largest_loss_pct=largest_loss_pct,
    )

    newest_first = list(reversed(closed_rows))
    truncated = len(newest_first) > CLOSED_POSITIONS_TABLE_LIMIT
    closed_positions = tuple(_to_record(r) for r in newest_first[:CLOSED_POSITIONS_TABLE_LIMIT])

    return StatisticsResult(
        since=since,
        until=until,
        summary=summary,
        pnl_curve=tuple(pnl_curve),
        closed_positions=closed_positions,
        closed_positions_truncated=truncated,
    )
