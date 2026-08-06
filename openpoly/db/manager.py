"""Database runtime manager.

Owns the persistence layer's runtime objects — the SQLAlchemy engine and the
two write-behind writers (order book + news). Lifted out of the FastAPI
lifespan so the ``database`` section has a manager to back it, mirroring
``MarketSourceManager`` / ``NewsSourceManager``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine, func, select, text

from openpoly.db.book_store import make_order_book_sink
from openpoly.db.engine import get_engine, init_db, make_session_factory
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
    NewsItemRow,
    OrderBookSnapshot,
    PositionRow,
    SettlementDecisionRow,
)
from openpoly.db.writer import WriteBehindWriter
from openpoly.markets.models import OrderBook
from openpoly.news.ring_buffer import NewsItem
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    ExitDecision,
    SettlementDecision,
)

logger = logging.getLogger(__name__)


def _ensure_fill_live_columns(engine: Engine) -> None:
    """Idempotent migration: add order_id / tx_hash columns to fill table if
    they are missing (older DBs predate slice C). New DBs get the columns
    via init_db()'s create_all and skip this entirely.

    SQLite's ALTER TABLE ADD COLUMN only fails if the column exists, so we
    PRAGMA-check first instead of catching."""
    with engine.begin() as conn:
        existing = {r[1] for r in conn.execute(text("PRAGMA table_info(fill)")).fetchall()}
        if "order_id" not in existing:
            conn.execute(text("ALTER TABLE fill ADD COLUMN order_id VARCHAR"))
            logger.info("migration: added fill.order_id")
        if "tx_hash" not in existing:
            conn.execute(text("ALTER TABLE fill ADD COLUMN tx_hash VARCHAR"))
            logger.info("migration: added fill.tx_hash")


def _ensure_entry_decision_live_columns(engine: Engine) -> None:
    """Idempotent migration: add the signals_json column to entry_decision if
    missing (older DBs predate the entry-signals surfacing). New DBs get the
    column via init_db()'s create_all and skip this entirely."""
    with engine.begin() as conn:
        existing = {
            r[1] for r in conn.execute(text("PRAGMA table_info(entry_decision)")).fetchall()
        }
        if "signals_json" not in existing:
            conn.execute(text("ALTER TABLE entry_decision ADD COLUMN signals_json TEXT"))
            logger.info("migration: added entry_decision.signals_json")


def _ensure_analyzer_call_live_columns(engine: Engine) -> None:
    """Idempotent migration: add the self_check column to analyzer_call if
    missing (older DBs predate the self-check surfacing). New DBs get the
    column via init_db()'s create_all and skip this entirely."""
    with engine.begin() as conn:
        existing = {
            r[1] for r in conn.execute(text("PRAGMA table_info(analyzer_call)")).fetchall()
        }
        if "self_check" not in existing:
            conn.execute(text("ALTER TABLE analyzer_call ADD COLUMN self_check TEXT"))
            logger.info("migration: added analyzer_call.self_check")


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

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._book_writer: WriteBehindWriter | None = None
        self._news_writer: WriteBehindWriter | None = None
        self._embedding_call_writer: WriteBehindWriter | None = None
        self._analyzer_call_writer: WriteBehindWriter | None = None
        self._entry_decision_writer: WriteBehindWriter | None = None
        self._exit_decision_writer: WriteBehindWriter | None = None
        self._settlement_decision_writer: WriteBehindWriter | None = None

    # ---------- lifecycle ----------

    async def start(self, engine: Engine | None = None) -> None:
        """Create the engine + tables + write-behind writers and start them.

        ``engine`` overrides the process engine — tests pass a throwaway one.
        """
        self._engine = engine or get_engine()
        init_db(self._engine)
        _ensure_fill_live_columns(self._engine)
        _ensure_entry_decision_live_columns(self._engine)
        _ensure_analyzer_call_live_columns(self._engine)
        factory = make_session_factory(self._engine)
        self._book_writer = WriteBehindWriter(make_order_book_sink(factory))
        self._news_writer = WriteBehindWriter(make_news_sink(factory))
        self._embedding_call_writer = WriteBehindWriter(make_embedding_call_sink(factory))
        self._analyzer_call_writer = WriteBehindWriter(make_analyzer_call_sink(factory))
        self._entry_decision_writer = WriteBehindWriter(make_entry_decision_sink(factory))
        self._exit_decision_writer = WriteBehindWriter(make_exit_decision_sink(factory))
        self._settlement_decision_writer = WriteBehindWriter(make_settlement_decision_sink(factory))
        await self._book_writer.start()
        await self._news_writer.start()
        await self._embedding_call_writer.start()
        await self._analyzer_call_writer.start()
        await self._entry_decision_writer.start()
        await self._exit_decision_writer.start()
        await self._settlement_decision_writer.start()

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
            },
        }

    def _table_counts(self) -> dict[str, int]:
        if self._engine is None:
            return {}
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
