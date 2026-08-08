"""Database runtime manager.

Owns the persistence layer's runtime objects — the SQLAlchemy engine and the
two write-behind writers (order book + news). Lifted out of the FastAPI
lifespan so the ``database`` section has a manager to back it, mirroring
``MarketSourceManager`` / ``NewsSourceManager``.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine, func, select, text

from openpoly.db.book_store import make_order_book_sink
from openpoly.db.engine import get_engine, init_db, make_session_factory
from openpoly.db.market_catalog_store import make_market_catalog_sink
from openpoly.db.news_store import make_news_sink
from openpoly.db.section_log_store import (
    make_analyzer_call_sink,
    make_embedding_call_sink,
    make_entry_decision_sink,
    make_exit_decision_sink,
    make_settlement_decision_sink,
)
from openpoly.db.tables import (
    AnalyzerCallRow,
    EmbeddingCallRow,
    EntryDecisionRow,
    ExitDecisionRow,
    FillRow,
    MarketCatalogRow,
    NewsItemRow,
    OrderBookSnapshot,
    PositionRow,
    SettlementDecisionRow,
)
from openpoly.db.writer import WriteBehindWriter
from openpoly.markets.models import Market, OrderBook
from openpoly.news.ring_buffer import NewsItem
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    ExitDecision,
    SettlementDecision,
)

logger = logging.getLogger(__name__)


# Columns added to a table after its initial release, keyed by table name.
# New DBs already get every column here via init_db()'s create_all; this
# manifest only matters for upgrading an existing DB file that predates a
# given column. Table/column/type here are fixed literals maintained in this
# file, never external input, so building SQL from them (SQLite's DDL can't
# bind identifiers as query parameters anyway) carries no injection risk.
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "fill": [("order_id", "VARCHAR"), ("tx_hash", "VARCHAR")],  # slice C
    "entry_decision": [("signals_json", "TEXT")],  # entry-signals surfacing
    "analyzer_call": [("self_check", "TEXT")],  # self-check surfacing
}


def _ensure_columns(engine: Engine) -> None:
    """Idempotent migration: add any column in ``_COLUMN_MIGRATIONS`` missing
    from an existing DB. SQLite's ALTER TABLE ADD COLUMN only fails if the
    column already exists, so we PRAGMA-check first instead of catching.

    Skips a manifest table that doesn't exist yet rather than erroring —
    real production always runs ``init_db()``'s create_all first (so every
    table in the manifest already exists by the time this runs), but a unit
    test exercising this in isolation against a deliberately minimal schema
    shouldn't have to stand up every table this manifest ever grows to
    cover."""
    with engine.begin() as conn:
        tables_present = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).fetchall()
        }
        for table, columns in _COLUMN_MIGRATIONS.items():
            if table not in tables_present:
                continue
            existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
            for column, sql_type in columns:
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
                    logger.info("migration: added %s.%s", table, column)


def _ensure_indexes(engine: Engine) -> None:
    """Idempotent migration: add/replace indexes for older DBs (new DBs get
    the current schema via init_db()'s create_all and skip this entirely).
    SQLite's CREATE/DROP INDEX IF EXISTS make this safe to re-run every
    startup, same discipline as the _ensure_*_live_columns migrations above.

    - market_catalog.condition_id: was unindexed, so every persisted-catalog
      fallback lookup (backtest_routes.py / portfolio_routes.py /
      statistics_routes.py, via market_catalog_row_by_condition_id) was a
      full-table scan against a table documented to grow unboundedly.
    - order_book_snapshot: replaces the old standalone token_id index with a
      composite (token_id, recorded_at) index matching the actual filter
      shape of every real query against this table (token_id equality +
      recorded_at comparison/order) — the standalone index is superseded via
      the leftmost-prefix rule, not just left redundant.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_market_catalog_condition_id "
                "ON market_catalog (condition_id)"
            )
        )
        conn.execute(text("DROP INDEX IF EXISTS ix_order_book_snapshot_token_id"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_order_book_snapshot_token_recorded "
                "ON order_book_snapshot (token_id, recorded_at)"
            )
        )


class DatabaseConfig(BaseModel):
    """Config for the ``database`` section.

    The DB is system infrastructure — no tunable params; the persistence
    wiring (one SQLite file, two write-behind writers) is fixed.
    """


class DatabaseManager:
    """Owns the persistence runtime: the engine + the two write-behind writers.

    Lifecycle (start / stop) is driven by the FastAPI lifespan. Backs the
    ``database`` section; ``status`` powers its inspector.
    """

    # status() is polled by both /api/inspect/db-status (Tables tab, every
    # 5s) and /api/health/detail — a short TTL cache on the ten-COUNT(*)
    # scan avoids paying for it twice when both land close together, without
    # meaningfully staling numbers that are themselves just a display.
    _TABLE_COUNTS_CACHE_TTL_SECONDS = 5.0

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._book_writer: WriteBehindWriter | None = None
        self._news_writer: WriteBehindWriter | None = None
        self._embedding_call_writer: WriteBehindWriter | None = None
        self._analyzer_call_writer: WriteBehindWriter | None = None
        self._entry_decision_writer: WriteBehindWriter | None = None
        self._exit_decision_writer: WriteBehindWriter | None = None
        self._settlement_decision_writer: WriteBehindWriter | None = None
        self._market_catalog_writer: WriteBehindWriter | None = None
        self._table_counts_cache: dict[str, int] | None = None
        self._table_counts_cached_at: float = 0.0

    # ---------- lifecycle ----------

    async def start(self, engine: Engine | None = None) -> None:
        """Create the engine + tables + write-behind writers and start them.

        ``engine`` overrides the process engine — tests pass a throwaway one.
        """
        self._engine = engine or get_engine()
        init_db(self._engine)
        _ensure_columns(self._engine)
        _ensure_indexes(self._engine)
        factory = make_session_factory(self._engine)
        self._book_writer = WriteBehindWriter(make_order_book_sink(factory))
        self._news_writer = WriteBehindWriter(make_news_sink(factory))
        self._embedding_call_writer = WriteBehindWriter(make_embedding_call_sink(factory))
        self._analyzer_call_writer = WriteBehindWriter(make_analyzer_call_sink(factory))
        self._entry_decision_writer = WriteBehindWriter(make_entry_decision_sink(factory))
        self._exit_decision_writer = WriteBehindWriter(make_exit_decision_sink(factory))
        self._settlement_decision_writer = WriteBehindWriter(make_settlement_decision_sink(factory))
        self._market_catalog_writer = WriteBehindWriter(make_market_catalog_sink(factory))
        await self._book_writer.start()
        await self._news_writer.start()
        await self._embedding_call_writer.start()
        await self._analyzer_call_writer.start()
        await self._entry_decision_writer.start()
        await self._exit_decision_writer.start()
        await self._settlement_decision_writer.start()
        await self._market_catalog_writer.start()

    async def stop(self) -> None:
        """Stop all writers, flushing whatever is still queued.

        Each writer is stopped independently — one raising must not skip the
        rest, or their still-queued rows would never get flushed either."""
        writers: tuple[tuple[str, WriteBehindWriter | None], ...] = (
            ("book", self._book_writer),
            ("news", self._news_writer),
            ("embedding_call", self._embedding_call_writer),
            ("analyzer_call", self._analyzer_call_writer),
            ("entry_decision", self._entry_decision_writer),
            ("exit_decision", self._exit_decision_writer),
            ("settlement_decision", self._settlement_decision_writer),
            ("market_catalog", self._market_catalog_writer),
        )
        for name, writer in writers:
            if writer is None:
                continue
            try:
                await writer.stop()
            except Exception:  # noqa: BLE001 — one writer's failure must not skip the rest
                logger.exception("database manager: %s writer stop() failed", name)

    async def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop()

    # ---------- persist hooks (wired into the source managers) ----------

    def enqueue_order_book(self, book: OrderBook) -> bool:
        """Queue one order book for write-behind persistence. Returns False if
        the manager has not started."""
        if self._book_writer is None:
            return False
        return self._book_writer.enqueue(book)

    def enqueue_news(self, item: NewsItem) -> bool:
        """Queue one news item for write-behind persistence."""
        if self._news_writer is None:
            return False
        return self._news_writer.enqueue(item)

    def enqueue_embedding_call(self, call: EmbeddingCall) -> bool:
        """Queue one embedding-filter call for write-behind persistence."""
        if self._embedding_call_writer is None:
            return False
        return self._embedding_call_writer.enqueue(call)

    def enqueue_analyzer_call(self, call: AnalyzerCall) -> bool:
        """Queue one analyzer call for write-behind persistence."""
        if self._analyzer_call_writer is None:
            return False
        return self._analyzer_call_writer.enqueue(call)

    def enqueue_entry_decision(self, call: EntryDecision) -> bool:
        """Queue one entry decision for write-behind persistence."""
        if self._entry_decision_writer is None:
            return False
        return self._entry_decision_writer.enqueue(call)

    def enqueue_exit_decision(self, call: ExitDecision) -> bool:
        """Queue one exit-monitor decision for write-behind persistence."""
        if self._exit_decision_writer is None:
            return False
        return self._exit_decision_writer.enqueue(call)

    def enqueue_settlement_decision(self, call: SettlementDecision) -> bool:
        """Queue one settlement-monitor decision for write-behind persistence."""
        if self._settlement_decision_writer is None:
            return False
        return self._settlement_decision_writer.enqueue(call)

    def enqueue_market_catalog(self, market: Market) -> bool:
        """Queue one market for write-behind identity persistence (upsert by
        market_id) — see MarketCatalogRow's docstring for why this exists."""
        if self._market_catalog_writer is None:
            return False
        return self._market_catalog_writer.enqueue(market)

    # ---------- status (powers the database section inspector) ----------

    def status(self) -> dict[str, Any]:
        """Snapshot of the persistence layer — table row counts + writer stats."""
        return {
            "tables": self._table_counts(),
            "writers": {
                "order_book": self._writer_stats(self._book_writer),
                "news": self._writer_stats(self._news_writer),
                "embedding_call": self._writer_stats(self._embedding_call_writer),
                "analyzer_call": self._writer_stats(self._analyzer_call_writer),
                "entry_decision": self._writer_stats(self._entry_decision_writer),
                "exit_decision": self._writer_stats(self._exit_decision_writer),
                "settlement_decision": self._writer_stats(self._settlement_decision_writer),
                "market_catalog": self._writer_stats(self._market_catalog_writer),
            },
        }

    def _table_counts(self) -> dict[str, int]:
        if self._engine is None:
            return {}
        now = time.monotonic()
        if (
            self._table_counts_cache is not None
            and now - self._table_counts_cached_at < self._TABLE_COUNTS_CACHE_TTL_SECONDS
        ):
            return self._table_counts_cache
        counts = self._query_table_counts()
        self._table_counts_cache = counts
        self._table_counts_cached_at = now
        return counts

    def _query_table_counts(self) -> dict[str, int]:
        with make_session_factory(self._engine)() as session:
            return {
                "order_book_snapshot": session.execute(
                    select(func.count()).select_from(OrderBookSnapshot)
                ).scalar_one(),
                "news_item": session.execute(
                    select(func.count()).select_from(NewsItemRow)
                ).scalar_one(),
                "fill": session.execute(select(func.count()).select_from(FillRow)).scalar_one(),
                "position": session.execute(
                    select(func.count()).select_from(PositionRow)
                ).scalar_one(),
                "embedding_call": session.execute(
                    select(func.count()).select_from(EmbeddingCallRow)
                ).scalar_one(),
                "analyzer_call": session.execute(
                    select(func.count()).select_from(AnalyzerCallRow)
                ).scalar_one(),
                "entry_decision": session.execute(
                    select(func.count()).select_from(EntryDecisionRow)
                ).scalar_one(),
                "exit_decision": session.execute(
                    select(func.count()).select_from(ExitDecisionRow)
                ).scalar_one(),
                "settlement_decision": session.execute(
                    select(func.count()).select_from(SettlementDecisionRow)
                ).scalar_one(),
                "market_catalog": session.execute(
                    select(func.count()).select_from(MarketCatalogRow)
                ).scalar_one(),
            }

    @staticmethod
    def _writer_stats(writer: WriteBehindWriter | None) -> dict[str, int] | None:
        if writer is None:
            return None
        return {
            "written": writer.written,
            "dropped": writer.dropped,
            "pending": writer.pending,
            "sink_errors": writer.sink_errors,
        }


# Module-level singleton; the FastAPI lifespan + the database section wire to this.
manager = DatabaseManager()
