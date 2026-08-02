"""Pipeline call-log persistence — the five ``openpoly.runtime.section_log``
dataclasses → their durable table mirrors, and the write-behind sinks each
feeds.

Every dataclass already exposes ``.to_dict()``, so each ``xxx_to_row``
conversion is a straight ``Row(**call.to_dict())`` — the table columns are
named identically to the dataclass fields, so there is no manual mapping to
keep in sync.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import (
    AnalyzerCallRow,
    EmbeddingCallRow,
    EntryDecisionRow,
    ExitDecisionRow,
    SettlementDecisionRow,
)
from openpoly.db.writer import Sink
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    ExitDecision,
    SettlementDecision,
)


def embedding_call_to_row(call: EmbeddingCall) -> EmbeddingCallRow:
    return EmbeddingCallRow(**call.to_dict())


def analyzer_call_to_row(call: AnalyzerCall) -> AnalyzerCallRow:
    return AnalyzerCallRow(**call.to_dict())


def entry_decision_to_row(call: EntryDecision) -> EntryDecisionRow:
    return EntryDecisionRow(**call.to_dict())


def exit_decision_to_row(call: ExitDecision) -> ExitDecisionRow:
    return ExitDecisionRow(**call.to_dict())


def settlement_decision_to_row(call: SettlementDecision) -> SettlementDecisionRow:
    return SettlementDecisionRow(**call.to_dict())


def _make_sink(to_row, session_factory: sessionmaker[Session]) -> Sink:
    """Build a write-behind sink that persists a batch of call dataclasses via
    ``to_row``. Sync (the writer runs it in a worker thread, off the loop).
    Drain calls are serialized, so one session per batch is safe."""

    def sink(calls: list) -> None:
        if not calls:
            return
        with session_factory() as session:
            session.add_all([to_row(c) for c in calls])
            session.commit()

    return sink


def make_embedding_call_sink(session_factory: sessionmaker[Session]) -> Sink:
    return _make_sink(embedding_call_to_row, session_factory)


def make_analyzer_call_sink(session_factory: sessionmaker[Session]) -> Sink:
    return _make_sink(analyzer_call_to_row, session_factory)


def make_entry_decision_sink(session_factory: sessionmaker[Session]) -> Sink:
    return _make_sink(entry_decision_to_row, session_factory)


def make_exit_decision_sink(session_factory: sessionmaker[Session]) -> Sink:
    return _make_sink(exit_decision_to_row, session_factory)


def make_settlement_decision_sink(session_factory: sessionmaker[Session]) -> Sink:
    return _make_sink(settlement_decision_to_row, session_factory)
