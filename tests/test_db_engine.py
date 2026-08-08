"""Tests for openpoly.db.engine — engine / session / bootstrap (SQLite)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from openpoly.db.engine import (
    DEFAULT_DB_URL,
    database_url,
    init_db,
    make_engine,
    make_session_factory,
)


def test_make_engine_and_session_smoke():
    engine = make_engine("sqlite:///:memory:")
    factory = make_session_factory(engine)
    with factory() as session:
        result = session.execute(text("SELECT 1")).scalar_one()
    assert result == 1
    engine.dispose()


def test_init_db_runs():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)  # creates registered tables; must not raise
    engine.dispose()


def test_database_url_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENPOLY_DB_URL", raising=False)
    assert database_url() == DEFAULT_DB_URL


def test_database_url_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENPOLY_DB_URL", "sqlite:///custom.db")
    assert database_url() == "sqlite:///custom.db"
    assert str(make_engine().url) == "sqlite:///custom.db"


def test_init_db_creates_fill_with_order_id_tx_hash(tmp_path) -> None:
    """Fresh DB: order_id + tx_hash columns exist via create_all."""
    from sqlalchemy import text

    engine = make_engine(f"sqlite:///{tmp_path}/x.db")
    init_db(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(fill)")).fetchall()}
    assert "order_id" in cols
    assert "tx_hash" in cols


def test_ensure_columns_migrates_old_fill_table(tmp_path) -> None:
    """Old DB without order_id/tx_hash: migration adds them, idempotent.
    Exercises _ensure_columns against a schema that only has `fill` (not
    entry_decision/analyzer_call) — the manifest-table-doesn't-exist-yet
    skip must not error on the tables this test never created."""
    from sqlalchemy import text
    from openpoly.db.manager import _ensure_columns

    engine = make_engine(f"sqlite:///{tmp_path}/x.db")
    # Simulate old schema by creating fill without the new columns
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE fill (
                id INTEGER PRIMARY KEY,
                ts FLOAT NOT NULL,
                market_id VARCHAR NOT NULL,
                side VARCHAR NOT NULL,
                action VARCHAR NOT NULL,
                price FLOAT NOT NULL,
                qty FLOAT NOT NULL,
                fee FLOAT NOT NULL,
                position_id INTEGER NOT NULL,
                news_id VARCHAR,
                "trigger" VARCHAR
            )
        """)
        )
    _ensure_columns(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(fill)")).fetchall()}
    assert "order_id" in cols
    assert "tx_hash" in cols
    # Second run is a no-op
    _ensure_columns(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(fill)")).fetchall()}
    assert "order_id" in cols
    assert "tx_hash" in cols


def test_ensure_columns_migrates_multiple_tables_in_one_call(tmp_path) -> None:
    """Old DB missing columns on two different manifest tables at once:
    one _ensure_columns call migrates both, idempotent."""
    from sqlalchemy import text
    from openpoly.db.manager import _ensure_columns

    engine = make_engine(f"sqlite:///{tmp_path}/x.db")
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE entry_decision (
                id INTEGER PRIMARY KEY,
                ts FLOAT NOT NULL,
                news_id VARCHAR NOT NULL,
                ar_p_model FLOAT,
                ar_market_id VARCHAR,
                verdict VARCHAR NOT NULL,
                side VARCHAR,
                qty FLOAT,
                price FLOAT,
                reason VARCHAR,
                latency_ms INTEGER NOT NULL,
                error VARCHAR,
                fill_status VARCHAR,
                fill_price FLOAT,
                fill_qty FLOAT,
                position_id INTEGER
            )
        """)
        )
        conn.execute(
            text("""
            CREATE TABLE analyzer_call (
                id INTEGER PRIMARY KEY,
                ts FLOAT NOT NULL,
                news_id VARCHAR NOT NULL,
                verdict VARCHAR NOT NULL
            )
        """)
        )
    _ensure_columns(engine)
    with engine.begin() as conn:
        entry_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(entry_decision)")).fetchall()
        }
        analyzer_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(analyzer_call)")).fetchall()
        }
    assert "signals_json" in entry_cols
    assert "self_check" in analyzer_cols
    # Second run is a no-op
    _ensure_columns(engine)
    with engine.begin() as conn:
        entry_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(entry_decision)")).fetchall()
        }
    assert "signals_json" in entry_cols


def test_ensure_indexes_migrates_old_db(tmp_path) -> None:
    """Old DB with only the pre-migration schema (market_catalog.condition_id
    unindexed, order_book_snapshot indexed on token_id alone): migration adds
    the condition_id index, replaces the standalone token_id index with the
    composite (token_id, recorded_at) one, and is idempotent."""
    from sqlalchemy import inspect
    from openpoly.db.manager import _ensure_indexes

    engine = make_engine(f"sqlite:///{tmp_path}/x.db")
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE market_catalog (
                market_id VARCHAR PRIMARY KEY,
                condition_id VARCHAR NOT NULL,
                question VARCHAR NOT NULL,
                slug VARCHAR NOT NULL,
                yes_token_id VARCHAR NOT NULL,
                no_token_id VARCHAR,
                neg_risk BOOLEAN NOT NULL,
                first_seen_at FLOAT NOT NULL,
                last_seen_at FLOAT NOT NULL
            )
        """)
        )
        conn.execute(
            text("""
            CREATE TABLE order_book_snapshot (
                id INTEGER PRIMARY KEY,
                token_id VARCHAR NOT NULL,
                recorded_at FLOAT NOT NULL,
                bids_json TEXT NOT NULL,
                asks_json TEXT NOT NULL
            )
        """)
        )
        conn.execute(
            text(
                "CREATE INDEX ix_order_book_snapshot_token_id "
                "ON order_book_snapshot (token_id)"
            )
        )

    def index_names(table: str) -> set[str]:
        return {idx["name"] for idx in inspect(engine).get_indexes(table)}

    assert index_names("market_catalog") == set()
    assert index_names("order_book_snapshot") == {"ix_order_book_snapshot_token_id"}

    _ensure_indexes(engine)
    assert index_names("market_catalog") == {"ix_market_catalog_condition_id"}
    assert index_names("order_book_snapshot") == {"ix_order_book_snapshot_token_recorded"}

    # Second run is a no-op, not an error.
    _ensure_indexes(engine)
    assert index_names("market_catalog") == {"ix_market_catalog_condition_id"}
    assert index_names("order_book_snapshot") == {"ix_order_book_snapshot_token_recorded"}
