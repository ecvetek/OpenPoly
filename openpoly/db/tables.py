"""SQLAlchemy ORM table definitions — the single SQLite database's schema."""

from __future__ import annotations

from sqlalchemy import Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from openpoly.db.engine import Base


class MarketCatalogRow(Base):
    """Durable market identity — upserted (one row per ``market_id``) on
    every discovery poll and every holding-sync fetch, independent of
    whether the market is still in the live discovery catalog.

    ``order_book_snapshot`` already persists price history forever with no
    pruning, but a market's *identity* (question, token ids, condition_id)
    used to live only in the in-memory ``MarketStore`` — gone the moment a
    market fell out of live discovery (resolved, expired, thinned out by a
    filter change). That made a backtest unable to resolve any historical
    ``AnalyzerCallRow`` whose market had since left the catalog, even though
    its price history was still sitting right there in
    ``order_book_snapshot`` — surfaced as a growing
    ``skipped_market_not_in_catalog`` count on longer backtest ranges. This
    table is the fix: durable identity, independent of live discovery
    state, so a backtest can resolve any market it has ever seen a poll for.

    No pruning — a row is a couple hundred bytes; even tens of thousands of
    markets ever discovered is negligible next to the already-unbounded
    ``order_book_snapshot`` table, so there is no 30-day-window bookkeeping
    to get wrong. ``first_seen_at``/``last_seen_at`` are kept for visibility,
    not retention logic.
    """

    __tablename__ = "market_catalog"

    market_id: Mapped[str] = mapped_column(primary_key=True)
    # Indexed: market_catalog_row_by_condition_id (db/history_query.py) is
    # the persisted-catalog fallback backtest_routes.py / portfolio_routes.py
    # / statistics_routes.py all resolve a position's market through — an
    # unindexed lookup here is a full-table scan on every one of those calls,
    # against a table that's documented above as growing unboundedly.
    condition_id: Mapped[str] = mapped_column(index=True)
    question: Mapped[str]
    slug: Mapped[str]
    yes_token_id: Mapped[str]
    no_token_id: Mapped[str | None]
    neg_risk: Mapped[bool]
    first_seen_at: Mapped[float]
    last_seen_at: Mapped[float]


class OrderBookSnapshot(Base):
    """One sampled order book — top-N depth levels per side, stored as JSON.

    A time series: one row per market per book-sampling cycle. ``bids_json`` /
    ``asks_json`` hold ``[[price, size], ...]`` best-first — the depth ladder,
    not a quote snapshot (size is what makes walk-book / slippage answerable).
    """

    __tablename__ = "order_book_snapshot"
    __table_args__ = (
        # Every real query against this table (order_book_at_or_before,
        # order_book_snapshots_for_token, the inspect route's per-token
        # history) filters token_id equality + a recorded_at comparison or
        # order — the ideal shape for one composite index, which also serves
        # a bare token_id filter via the leftmost-prefix rule. Supersedes a
        # standalone index on token_id alone (removed below), highest-volume,
        # never-pruned table (order_book history persists forever) and now
        # also the backtest replay's hot path.
        Index("ix_order_book_snapshot_token_recorded", "token_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[str]
    recorded_at: Mapped[float]  # epoch seconds, UTC (the OrderBook.ts)
    bids_json: Mapped[str]
    asks_json: Mapped[str]


class NewsItemRow(Base):
    """One persisted news item — the durable mirror of the in-memory news ring.

    Surrogate ``id`` PK (not ``news_id``): the write-behind sink must never
    crash on a rare upstream-dedup miss, so duplicates are tolerated rather
    than raising an integrity error.
    """

    __tablename__ = "news_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[str] = mapped_column(index=True)
    content: Mapped[str]
    urgency: Mapped[str]
    # Free-form categorical label from upstream ('neutral' / 'positive' / ...),
    # treated as text like ``urgency`` — never coerced to a number. SQLite's
    # dynamic typing stores text into this column even if an older DB created it
    # with NUMERIC affinity, so no migration is needed.
    sentiment: Mapped[str | None]
    published_at: Mapped[float]
    received_at: Mapped[float] = mapped_column(index=True)


class MarketEmbeddingRow(Base):
    """One cached sentence embedding for a market ``question``.

    The durable backing for ``EmbeddingManager``'s in-memory vector dict — it
    lets a process restart reload the catalog's embeddings instead of paying
    the cold-start recompute. ``vector`` is a float32 ndarray serialized to
    bytes; ``text_hash`` is a digest of the encoded ``question``, so a
    re-titled market invalidates its stale vector. Rows are unique per
    (market, model): switching the embedding model naturally misses and
    recomputes rather than reading a dimension-mismatched vector.
    """

    __tablename__ = "market_embedding"
    __table_args__ = (UniqueConstraint("market_id", "model_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(index=True)
    model_name: Mapped[str]
    text_hash: Mapped[str]
    vector: Mapped[bytes]
    created_at: Mapped[float]  # epoch seconds, UTC (our wall clock at cache write)


class FillRow(Base):
    """One executed fill — an append-only ledger row, the portfolio's source of
    truth.

    Every buy and sell is one immutable row; the ``position`` table is a
    materialized projection that can always be rebuilt by folding fills.
    ``news_id`` traces a buy back to its triggering news; ``trigger`` records
    why a sell fired. ``fee`` is 0 under the zero-fee rule.
    """

    __tablename__ = "fill"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float]  # epoch seconds, UTC
    market_id: Mapped[str] = mapped_column(index=True)
    side: Mapped[str]  # yes | no
    action: Mapped[str]  # buy | sell
    price: Mapped[float]
    qty: Mapped[float]
    fee: Mapped[float]
    position_id: Mapped[int] = mapped_column(index=True)
    news_id: Mapped[str | None]
    trigger: Mapped[str | None]
    order_id: Mapped[str | None] = mapped_column(default=None)  # NEW (slice C)
    tx_hash: Mapped[str | None] = mapped_column(default=None)  # NEW (slice C)


class PositionSignalRow(Base):
    """One news/analyzer decision attached to a position — append-only.

    The buy fill's ``news_id`` records the ONE news item that opened a
    position; this table records every *further* decision the pipeline made
    about the same market while that position was open. ``relation`` says
    which: ``reinforce`` (wanted the held side again — the entry was blocked
    by the one-position-per-(market, side) rule) or ``contradict`` (wanted the
    opposite side of the same market). The opening decision is recorded here
    too, as ``opening``, so the ledger is self-contained.

    ``side`` is the side the decision WANTED, which for ``contradict`` is not
    the side the position holds. ``p_model`` / ``confidence`` are snapshotted
    from the analyzer so the exit tick needn't join ``analyzer_call``.

    Derived state (``solo`` / ``reinforced`` / ``contested``) is computed from
    these rows by ``openpoly.portfolio.confluence.evaluate`` — nothing is
    materialized onto ``position``, same discipline as ``fill`` → ``position``.
    """

    __tablename__ = "position_signal"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(index=True)
    news_id: Mapped[str] = mapped_column(index=True)
    ts: Mapped[float]  # epoch seconds, UTC
    side: Mapped[str]  # yes | no — the side the decision wanted
    relation: Mapped[str]  # opening | reinforce | contradict
    p_model: Mapped[float | None]
    confidence: Mapped[str | None]  # low | medium | high


class EmbeddingCallRow(Base):
    """One persisted embedding-filter call — durable mirror of the in-memory
    ``embedding_log`` ring (``openpoly.runtime.section_log``).

    Surrogate ``id`` PK, event-log style like ``NewsItemRow``: one row per
    call, never updated, duplicates tolerated.
    """

    __tablename__ = "embedding_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(index=True)
    news_id: Mapped[str] = mapped_column(index=True)
    news_content_preview: Mapped[str]
    urgency: Mapped[str]
    verdict: Mapped[str]
    candidate_count: Mapped[int]
    top_market_id: Mapped[str | None]
    top_score: Mapped[float | None]
    catalog_size: Mapped[int]
    latency_ms: Mapped[int]
    error: Mapped[str | None]


class AnalyzerCallRow(Base):
    """One persisted analyzer call — durable mirror of the in-memory
    ``analyzer_log`` ring. ``news_id`` is the column ``PositionDetail``'s
    rationale lookup filters on."""

    __tablename__ = "analyzer_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(index=True)
    news_id: Mapped[str] = mapped_column(index=True)
    news_content_preview: Mapped[str]
    urgency: Mapped[str]
    verdict: Mapped[str]
    p_model: Mapped[float | None]
    confidence: Mapped[str | None]
    market_id: Mapped[str | None]
    latency_ms: Mapped[int]
    error: Mapped[str | None]
    rationale: Mapped[str | None]
    self_check: Mapped[str | None]


class EntryDecisionRow(Base):
    """One persisted entry decision — durable mirror of the in-memory
    ``entry_log`` ring."""

    __tablename__ = "entry_decision"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(index=True)
    news_id: Mapped[str] = mapped_column(index=True)
    ar_p_model: Mapped[float | None]
    ar_market_id: Mapped[str | None]
    verdict: Mapped[str]
    side: Mapped[str | None]
    qty: Mapped[float | None]
    price: Mapped[float | None]
    reason: Mapped[str | None]
    latency_ms: Mapped[int]
    error: Mapped[str | None]
    fill_status: Mapped[str | None]
    fill_price: Mapped[float | None]
    fill_qty: Mapped[float | None]
    position_id: Mapped[int | None] = mapped_column(index=True)
    signals_json: Mapped[str | None]


class ExitDecisionRow(Base):
    """One persisted exit-monitor evaluation — durable mirror of the
    in-memory ``exit_log`` ring."""

    __tablename__ = "exit_decision"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(index=True)
    position_id: Mapped[int] = mapped_column(index=True)
    market_id: Mapped[str] = mapped_column(index=True)
    side: Mapped[str]
    verdict: Mapped[str]
    trigger: Mapped[str | None]
    return_pct: Mapped[float | None]
    fill_price: Mapped[float | None]
    realized_pnl: Mapped[float | None]
    reason: Mapped[str | None]
    error: Mapped[str | None]
    peak_price: Mapped[float | None]


class SettlementDecisionRow(Base):
    """One persisted settlement-monitor evaluation — durable mirror of the
    in-memory ``settlement_log`` ring."""

    __tablename__ = "settlement_decision"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(index=True)
    position_id: Mapped[int] = mapped_column(index=True)
    market_id: Mapped[str] = mapped_column(index=True)
    side: Mapped[str]
    verdict: Mapped[str]
    final_price: Mapped[float | None]
    realized_pnl: Mapped[float | None]
    reason: Mapped[str | None]
    error: Mapped[str | None]


class PositionRow(Base):
    """One position — a materialized projection of the ``fill`` ledger.

    openPoly is one-shot per (market, side): a position is exactly one buy fill
    and later one sell fill, so ``qty`` / ``avg_entry_price`` equal that buy
    fill with no weighted-average recompute. ``realized_pnl`` is set once at
    close and is itself derivable from the two fills. ``token_id`` is stored so
    a close can read the order book without the market still being in the live
    catalog. The partial unique index allows at most one ``open`` position per
    (market_id, side) — re-entry after a close is fine (closed rows fall
    outside the index).
    """

    __tablename__ = "position"
    __table_args__ = (
        Index(
            "ix_position_open_unique",
            "market_id",
            "side",
            unique=True,
            sqlite_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(index=True)
    side: Mapped[str]  # yes | no
    token_id: Mapped[str]
    condition_id: Mapped[str]
    qty: Mapped[float]
    avg_entry_price: Mapped[float]
    status: Mapped[str]  # open | closed
    opened_at: Mapped[float]  # epoch seconds, UTC
    closed_at: Mapped[float | None]
    close_reason: Mapped[str | None]
    realized_pnl: Mapped[float | None]
