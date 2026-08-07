"""Read-side range / point queries over persisted history.

Kept separate from ``book_store.py`` (write-focused — see its own docstring)
and from the ad-hoc inline SQL every current caller of these tables writes
for itself (``portfolio/equity.py``'s boundary-seed query,
``runtime/exit_monitor.py``'s ``bootstrap_peaks``,
``api/inspect_routes.py``'s bounded range read, ``api/runtime_routes.py``'s
newest-N read). Built for ``openpoly.backtest``'s replay engine, which needs
both an "as-of a point in time" lookup and a verdict-filtered time range —
neither existed as a reusable query before.

Functions here take an already-open ``Session`` rather than a
``session_factory`` — the backtest engine keeps one session open across an
entire replay (thousands of point-queries) rather than paying a session
open/close per lookup.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from openpoly.db.tables import AnalyzerCallRow, MarketCatalogRow, OrderBookSnapshot


def order_book_at_or_before(session: Session, token_id: str, ts: float) -> OrderBookSnapshot | None:
    """The most recent ``OrderBookSnapshot`` row for ``token_id`` at or
    before ``ts``, or ``None`` if no snapshot exists yet at that point. Same
    at-or-before idiom ``portfolio/equity.py``'s boundary-seed query already
    uses."""
    stmt = (
        select(OrderBookSnapshot)
        .where(OrderBookSnapshot.token_id == token_id, OrderBookSnapshot.recorded_at <= ts)
        .order_by(OrderBookSnapshot.recorded_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def analyzer_calls_in_range(
    session: Session, since: float, until: float, *, verdict: str = "ok"
) -> list[AnalyzerCallRow]:
    """``AnalyzerCallRow`` rows with the given ``verdict`` in the half-open
    ``[since, until)`` window, ascending by ``ts`` — matches
    ``portfolio/statistics.py``'s half-open convention, not
    ``inspect_routes.py``'s inclusive-both-ends one."""
    stmt = (
        select(AnalyzerCallRow)
        .where(
            AnalyzerCallRow.ts >= since,
            AnalyzerCallRow.ts < until,
            AnalyzerCallRow.verdict == verdict,
        )
        .order_by(AnalyzerCallRow.ts.asc())
    )
    return list(session.execute(stmt).scalars().all())


def market_catalog_row(session: Session, market_id: str) -> MarketCatalogRow | None:
    """The durable identity row for ``market_id``, or ``None`` if it was
    never captured by a discovery poll or holding-sync fetch. Simple PK
    lookup — ``market_catalog`` has at most one row per market_id."""
    return session.get(MarketCatalogRow, market_id)


def market_catalog_row_by_condition_id(
    session: Session, condition_id: str
) -> MarketCatalogRow | None:
    """Same as ``market_catalog_row`` but keyed by on-chain ``condition_id``
    — what ``PositionRecord`` stores, not ``market_id`` — for resolving a
    backtest position's market_question when the market isn't in the live
    catalog (see ``api/backtest_routes.py``'s ``_lookup_market``)."""
    stmt = select(MarketCatalogRow).where(MarketCatalogRow.condition_id == condition_id).limit(1)
    return session.execute(stmt).scalars().first()


def order_book_snapshots_for_token(
    session: Session, token_id: str, since: float, until: float
) -> list[OrderBookSnapshot]:
    """Every snapshot for ``token_id`` in ``[since, until)``, ascending by
    ``recorded_at`` — the per-position price path the exit replay walks,
    finer-grained than any fixed tick cadence since it only ever visits
    timestamps the data actually changed at."""
    stmt = (
        select(OrderBookSnapshot)
        .where(
            OrderBookSnapshot.token_id == token_id,
            OrderBookSnapshot.recorded_at >= since,
            OrderBookSnapshot.recorded_at < until,
        )
        .order_by(OrderBookSnapshot.recorded_at.asc())
    )
    return list(session.execute(stmt).scalars().all())
