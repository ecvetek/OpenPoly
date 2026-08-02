"""Tests for openpoly.db.section_log_store — pipeline call-log persistence
(embedding / analyzer / entry / exit / settlement)."""

from __future__ import annotations

from sqlalchemy import select

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.section_log_store import (
    analyzer_call_to_row,
    embedding_call_to_row,
    entry_decision_to_row,
    exit_decision_to_row,
    make_analyzer_call_sink,
    make_embedding_call_sink,
    make_entry_decision_sink,
    make_exit_decision_sink,
    make_settlement_decision_sink,
    settlement_decision_to_row,
)
from openpoly.db.tables import (
    AnalyzerCallRow,
    EmbeddingCallRow,
    EntryDecisionRow,
    ExitDecisionRow,
    SettlementDecisionRow,
)
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    ExitDecision,
    SettlementDecision,
)


def _engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'calls.db'}")
    init_db(engine)
    return engine


def _embedding_call(news_id: str = "n1") -> EmbeddingCall:
    return EmbeddingCall(
        ts=1.0,
        news_id=news_id,
        news_content_preview="preview",
        urgency="high",
        verdict="ok",
        candidate_count=2,
        top_market_id="m1",
        top_score=0.9,
        catalog_size=40,
        latency_ms=12,
    )


def _analyzer_call(news_id: str = "n1") -> AnalyzerCall:
    return AnalyzerCall(
        ts=1.0,
        news_id=news_id,
        news_content_preview="preview",
        urgency="high",
        verdict="ok",
        p_model=0.55,
        confidence="medium",
        market_id="m1",
        latency_ms=20,
        rationale="because reasons",
    )


def _entry_decision(news_id: str = "n1") -> EntryDecision:
    return EntryDecision(
        ts=1.0,
        news_id=news_id,
        ar_p_model=0.55,
        ar_market_id="m1",
        verdict="ok",
        side="yes",
        qty=20.0,
        price=0.5,
        reason=None,
        latency_ms=15,
        fill_status="filled",
        fill_price=0.5,
        fill_qty=20.0,
        position_id=1,
    )


def _exit_decision(position_id: int = 1) -> ExitDecision:
    return ExitDecision(
        ts=1.0,
        position_id=position_id,
        market_id="m1",
        side="yes",
        verdict="ok",
        trigger="take_profit",
        return_pct=0.25,
        fill_price=0.62,
        realized_pnl=2.1,
        reason="take_profit",
        peak_price=0.63,
    )


def _settlement_decision(position_id: int = 1) -> SettlementDecision:
    return SettlementDecision(
        ts=1.0,
        position_id=position_id,
        market_id="m1",
        side="yes",
        verdict="ok",
        final_price=1.0,
        realized_pnl=6.0,
        reason="settlement",
    )


# ---------- to_row ----------


def test_embedding_call_to_row():
    row = embedding_call_to_row(_embedding_call("e1"))
    assert row.news_id == "e1"
    assert row.candidate_count == 2
    assert row.top_market_id == "m1"


def test_analyzer_call_to_row():
    row = analyzer_call_to_row(_analyzer_call("a1"))
    assert row.news_id == "a1"
    assert row.p_model == 0.55
    assert row.rationale == "because reasons"


def test_entry_decision_to_row():
    row = entry_decision_to_row(_entry_decision("t1"))
    assert row.news_id == "t1"
    assert row.side == "yes"
    assert row.position_id == 1


def test_exit_decision_to_row():
    row = exit_decision_to_row(_exit_decision(7))
    assert row.position_id == 7
    assert row.trigger == "take_profit"
    assert row.realized_pnl == 2.1


def test_settlement_decision_to_row():
    row = settlement_decision_to_row(_settlement_decision(7))
    assert row.position_id == 7
    assert row.final_price == 1.0


# ---------- sinks ----------


def test_embedding_call_sink_persists_batch(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_embedding_call_sink(factory)([_embedding_call("a"), _embedding_call("b")])
    with factory() as session:
        rows = session.execute(select(EmbeddingCallRow)).scalars().all()
    assert {r.news_id for r in rows} == {"a", "b"}
    engine.dispose()


def test_analyzer_call_sink_persists_batch(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_analyzer_call_sink(factory)([_analyzer_call("a")])
    with factory() as session:
        rows = session.execute(select(AnalyzerCallRow)).scalars().all()
    assert [r.news_id for r in rows] == ["a"]
    assert rows[0].rationale == "because reasons"
    engine.dispose()


def test_entry_decision_sink_persists_batch(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_entry_decision_sink(factory)([_entry_decision("a")])
    with factory() as session:
        rows = session.execute(select(EntryDecisionRow)).scalars().all()
    assert [r.news_id for r in rows] == ["a"]
    engine.dispose()


def test_exit_decision_sink_persists_batch(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_exit_decision_sink(factory)([_exit_decision(1), _exit_decision(2)])
    with factory() as session:
        rows = session.execute(select(ExitDecisionRow)).scalars().all()
    assert {r.position_id for r in rows} == {1, 2}
    engine.dispose()


def test_settlement_decision_sink_persists_batch(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_settlement_decision_sink(factory)([_settlement_decision(1)])
    with factory() as session:
        rows = session.execute(select(SettlementDecisionRow)).scalars().all()
    assert [r.position_id for r in rows] == [1]
    engine.dispose()


def test_sink_empty_batch_is_noop(tmp_path):
    engine = _engine(tmp_path)
    factory = make_session_factory(engine)
    make_analyzer_call_sink(factory)([])
    with factory() as session:
        assert session.execute(select(AnalyzerCallRow)).all() == []
    engine.dispose()
