"""Tests for openpoly.db.manager — DatabaseManager."""

from __future__ import annotations

from sqlalchemy import select

from openpoly.db.engine import make_engine, make_session_factory
from openpoly.db.manager import DatabaseManager
from openpoly.db.tables import AnalyzerCallRow, NewsItemRow, OrderBookSnapshot
from openpoly.markets.models import OrderBook
from openpoly.news.ring_buffer import NewsItem
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    ExitDecision,
    SettlementDecision,
)


def _engine(tmp_path):
    return make_engine(f"sqlite:///{tmp_path / 'mgr.db'}")


def _book(token_id: str) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=[(0.40, 10.0)], asks=[(0.42, 8.0)])


def _news(news_id: str) -> NewsItem:
    return NewsItem(
        id=news_id,
        content=f"c-{news_id}",
        urgency="high",
        sentiment=None,
        published_at=1.0,
        received_at=2.0,
    )


def _embedding_call(news_id: str = "n1") -> EmbeddingCall:
    return EmbeddingCall(
        ts=1.0,
        news_id=news_id,
        news_content_preview="p",
        urgency="high",
        verdict="ok",
        candidate_count=1,
        top_market_id="m1",
        top_score=0.9,
        catalog_size=10,
        latency_ms=5,
    )


def _analyzer_call(news_id: str = "n1") -> AnalyzerCall:
    return AnalyzerCall(
        ts=1.0,
        news_id=news_id,
        news_content_preview="p",
        urgency="high",
        verdict="ok",
        p_model=0.5,
        confidence="medium",
        market_id="m1",
        latency_ms=10,
        rationale="r",
    )


def _entry_decision(news_id: str = "n1") -> EntryDecision:
    return EntryDecision(
        ts=1.0,
        news_id=news_id,
        ar_p_model=0.5,
        ar_market_id="m1",
        verdict="ok",
        side="yes",
        qty=10.0,
        price=0.5,
        reason=None,
        latency_ms=8,
    )


def _exit_decision(position_id: int = 1) -> ExitDecision:
    return ExitDecision(
        ts=1.0,
        position_id=position_id,
        market_id="m1",
        side="yes",
        verdict="ok",
        trigger="take_profit",
        return_pct=0.2,
        fill_price=0.6,
        realized_pnl=1.0,
        reason="take_profit",
    )


def _settlement_decision(position_id: int = 1) -> SettlementDecision:
    return SettlementDecision(
        ts=1.0,
        position_id=position_id,
        market_id="m1",
        side="yes",
        verdict="ok",
        final_price=1.0,
        realized_pnl=1.0,
        reason="settlement",
    )


def test_status_before_start_is_empty():
    status = DatabaseManager().status()
    assert status["tables"] == {}
    assert status["writers"]["order_book"] is None
    assert status["writers"]["news"] is None
    assert status["writers"]["embedding_call"] is None
    assert status["writers"]["analyzer_call"] is None
    assert status["writers"]["entry_decision"] is None
    assert status["writers"]["exit_decision"] is None
    assert status["writers"]["settlement_decision"] is None


def test_enqueue_before_start_returns_false():
    mgr = DatabaseManager()
    assert mgr.enqueue_order_book(_book("t")) is False
    assert mgr.enqueue_news(_news("n")) is False
    assert mgr.enqueue_embedding_call(_embedding_call()) is False
    assert mgr.enqueue_analyzer_call(_analyzer_call()) is False
    assert mgr.enqueue_entry_decision(_entry_decision()) is False
    assert mgr.enqueue_exit_decision(_exit_decision()) is False
    assert mgr.enqueue_settlement_decision(_settlement_decision()) is False


async def test_start_then_enqueue_persists(tmp_path):
    engine = _engine(tmp_path)
    mgr = DatabaseManager()
    await mgr.start(engine=engine)
    assert mgr.enqueue_order_book(_book("tok-a")) is True
    assert mgr.enqueue_news(_news("n1")) is True
    await mgr.stop()  # flush queued rows
    with make_session_factory(engine)() as session:
        books = session.execute(select(OrderBookSnapshot)).scalars().all()
        news = session.execute(select(NewsItemRow)).scalars().all()
    assert [b.token_id for b in books] == ["tok-a"]
    assert [n.news_id for n in news] == ["n1"]
    engine.dispose()


async def test_status_reports_table_counts(tmp_path):
    mgr = DatabaseManager()
    await mgr.start(engine=_engine(tmp_path))
    mgr.enqueue_order_book(_book("a"))
    mgr.enqueue_order_book(_book("b"))
    mgr.enqueue_news(_news("n1"))
    await mgr.stop()
    tables = mgr.status()["tables"]
    assert tables["order_book_snapshot"] == 2
    assert tables["news_item"] == 1


async def test_status_reports_writer_stats(tmp_path):
    mgr = DatabaseManager()
    await mgr.start(engine=_engine(tmp_path))
    mgr.enqueue_order_book(_book("a"))
    await mgr.stop()
    assert mgr.status()["writers"]["order_book"] == {
        "written": 1,
        "dropped": 0,
        "pending": 0,
    }


# ---------- pipeline call-log writers ----------


async def test_start_then_enqueue_call_logs_persists(tmp_path):
    engine = _engine(tmp_path)
    mgr = DatabaseManager()
    await mgr.start(engine=engine)
    assert mgr.enqueue_embedding_call(_embedding_call("e1")) is True
    assert mgr.enqueue_analyzer_call(_analyzer_call("a1")) is True
    assert mgr.enqueue_entry_decision(_entry_decision("t1")) is True
    assert mgr.enqueue_exit_decision(_exit_decision(1)) is True
    assert mgr.enqueue_settlement_decision(_settlement_decision(1)) is True
    await mgr.stop()  # flush queued rows

    from openpoly.db.tables import (
        EmbeddingCallRow,
        EntryDecisionRow,
        ExitDecisionRow,
        SettlementDecisionRow,
    )

    with make_session_factory(engine)() as session:
        assert [r.news_id for r in session.execute(select(EmbeddingCallRow)).scalars()] == ["e1"]
        assert [r.news_id for r in session.execute(select(AnalyzerCallRow)).scalars()] == ["a1"]
        assert [r.news_id for r in session.execute(select(EntryDecisionRow)).scalars()] == ["t1"]
        assert [r.position_id for r in session.execute(select(ExitDecisionRow)).scalars()] == [1]
        assert [
            r.position_id for r in session.execute(select(SettlementDecisionRow)).scalars()
        ] == [1]
    engine.dispose()


async def test_status_reports_call_log_table_counts(tmp_path):
    mgr = DatabaseManager()
    await mgr.start(engine=_engine(tmp_path))
    mgr.enqueue_analyzer_call(_analyzer_call("a1"))
    mgr.enqueue_analyzer_call(_analyzer_call("a2"))
    await mgr.stop()
    tables = mgr.status()["tables"]
    assert tables["analyzer_call"] == 2
    assert tables["embedding_call"] == 0


async def test_call_logs_survive_simulated_restart(tmp_path):
    """The exact scenario a real backend restart hits: enqueue + flush on one
    DatabaseManager, then build a *second, fresh* one on the same DB file
    (simulating a process restart) and confirm the data is still there."""
    db_path = tmp_path / "restart.db"
    engine1 = make_engine(f"sqlite:///{db_path}")
    mgr1 = DatabaseManager()
    await mgr1.start(engine=engine1)
    mgr1.enqueue_analyzer_call(_analyzer_call("survives-restart"))
    await mgr1.stop()
    engine1.dispose()

    engine2 = make_engine(f"sqlite:///{db_path}")
    mgr2 = DatabaseManager()
    await mgr2.start(engine=engine2)  # re-runs init_db — must be non-destructive
    with make_session_factory(engine2)() as session:
        rows = session.execute(select(AnalyzerCallRow)).scalars().all()
    assert [r.news_id for r in rows] == ["survives-restart"]
    await mgr2.stop()
    engine2.dispose()
