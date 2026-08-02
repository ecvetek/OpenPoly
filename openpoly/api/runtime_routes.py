"""Per-section log endpoints (v7 / P4, EM4 embedding stage).

``GET /api/{embedding,analyzer,entry,exit}/log`` expose the bounded ring +
counters + last_at for the inspector Calls / Decisions tabs. The routes share
a uniform response shape but carry section-specific entry payloads (loose
``dict[str, Any]`` to avoid duplicating dataclass field schemas — the
EmbeddingCall / AnalyzerCall / EntryDecision / ExitDecision dataclasses *are*
the contract, validated by their own unit tests). The exit log has no news
queue (``queue_depth`` is always ``0``); ``state`` mirrors the exit monitor's
loop state, and ``last_tick_at`` / ``open_positions`` / ``blocked`` carry its
tick heartbeat (within-threshold + no-order-book holds write no entry, so the
ring keeps only ok / error closes — these counts surface the rest).

``POST /api/analyzer/test`` is a connectivity probe — it builds an LLM client
from supplied analyzer-config fields and makes one minimal call, so the user
can verify the key / base URL / model before running the pipeline.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.engine import get_session_factory
from openpoly.db.tables import (
    AnalyzerCallRow,
    EmbeddingCallRow,
    EntryDecisionRow,
    ExitDecisionRow,
    SettlementDecisionRow,
)
from openpoly.llm import LLMClient, LLMError
from openpoly.runtime.orchestrator import get_orchestrator
from openpoly.runtime.section_log import (
    analyzer_log,
    embedding_log,
    embedding_warm_log,
    entry_log,
    exit_log,
    settlement_log,
)
from openpoly.runtime.exit_monitor import exit_monitor
from openpoly.runtime.settlement_monitor import settlement_monitor
from openpoly.sections.analyzer.llm_v0 import LLMAnalyzerConfig

router = APIRouter(prefix="/api", tags=["runtime"])


def _entries_from_db(
    factory: sessionmaker[Session], row_cls: type, limit: int
) -> list[dict[str, Any]]:
    """Newest ``limit`` rows from a persisted call-log table, returned
    oldest-first — matches ``SectionLogStore.entries(limit)``'s contract
    exactly (``limit<=0`` -> ``[]``)."""
    limit = max(0, limit)
    if limit == 0:
        return []
    with factory() as session:
        rows = (
            session.execute(
                select(row_cls).order_by(row_cls.ts.desc(), row_cls.id.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
    cols = [c.name for c in row_cls.__table__.columns if c.name != "id"]
    return [{col: getattr(r, col) for col in cols} for r in reversed(rows)]


class SectionLogResponse(BaseModel):
    entries: list[dict[str, Any]]
    counters: dict[str, int]
    last_at: float | None
    queue_depth: int
    state: str
    # Embedding-only: background warm-cache events (model load / cache reload /
    # warm cycles). None on the analyzer / entry routes — they have no warm loop.
    warm: list[dict[str, Any]] | None = None
    # Exit-only: tick heartbeat. None on the other routes. ``last_tick_at`` is
    # the last sweep's wall-clock; ``open_positions`` / ``blocked`` are that
    # sweep's counts (blocked == positions with no order book, can't evaluate).
    last_tick_at: float | None = None
    open_positions: int | None = None
    blocked: int | None = None


@router.get("/embedding/log", response_model=SectionLogResponse)
def get_embedding_log(
    limit: int = 200,
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> SectionLogResponse:
    orch = get_orchestrator()
    return SectionLogResponse(
        entries=_entries_from_db(factory, EmbeddingCallRow, limit),
        counters=embedding_log.counters(),
        last_at=embedding_log.last_at,
        queue_depth=orch.queue_depth,
        state=orch.state,
        warm=[e.to_dict() for e in embedding_warm_log.entries(limit=limit)],
    )


@router.get("/analyzer/log", response_model=SectionLogResponse)
def get_analyzer_log(
    limit: int = 200,
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> SectionLogResponse:
    orch = get_orchestrator()
    return SectionLogResponse(
        entries=_entries_from_db(factory, AnalyzerCallRow, limit),
        counters=analyzer_log.counters(),
        last_at=analyzer_log.last_at,
        queue_depth=orch.queue_depth,
        state=orch.state,
    )


# ---------- POST /api/analyzer/test — LLM connectivity probe ----------


class AnalyzerTestRequest(BaseModel):
    """The analyzer-config fields that affect connectivity. Mirrors the canvas
    node config; ``extra_guidance`` / ``min_confidence`` are irrelevant here."""

    llm_model: str
    api_key_ref: str
    base_url: str = ""
    temperature: float = 0.2


class AnalyzerTestResponse(BaseModel):
    ok: bool
    error: str | None = None
    latency_ms: int | None = None


@router.post("/analyzer/test", response_model=AnalyzerTestResponse)
def test_analyzer(req: AnalyzerTestRequest) -> AnalyzerTestResponse:
    """Verify the analyzer's LLM config with one minimal forced tool call.

    The request is run through ``LLMAnalyzerConfig`` first, so the probe uses
    the exact (validated + normalized) config the runtime would build — e.g.
    a ``base_url``'s trailing ``/v1`` is stripped the same way.
    """
    try:
        cfg = LLMAnalyzerConfig(
            llm_model=req.llm_model,
            api_key_ref=req.api_key_ref,
            base_url=req.base_url,
            temperature=req.temperature,
        )
    except ValidationError as exc:
        return AnalyzerTestResponse(ok=False, error=f"invalid config: {exc}")

    client = LLMClient(
        api_key_ref=cfg.api_key_ref,
        model=cfg.llm_model,
        temperature=cfg.temperature,
        base_url=cfg.base_url,
    )
    start = time.monotonic()
    try:
        client.ping()
    except LLMError as exc:
        return AnalyzerTestResponse(ok=False, error=str(exc))
    return AnalyzerTestResponse(ok=True, latency_ms=int((time.monotonic() - start) * 1000))


@router.get("/entry/log", response_model=SectionLogResponse)
def get_entry_log(
    limit: int = 200,
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> SectionLogResponse:
    orch = get_orchestrator()
    return SectionLogResponse(
        entries=_entries_from_db(factory, EntryDecisionRow, limit),
        counters=entry_log.counters(),
        last_at=entry_log.last_at,
        queue_depth=orch.queue_depth,
        state=orch.state,
    )


@router.get("/exit/log", response_model=SectionLogResponse)
def get_exit_log(
    limit: int = 200,
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> SectionLogResponse:
    # The exit monitor is position-driven, not a news queue — queue_depth has
    # no meaning (always 0). state + the tick-heartbeat fields come straight
    # off the monitor singleton.
    return SectionLogResponse(
        entries=_entries_from_db(factory, ExitDecisionRow, limit),
        counters=exit_log.counters(),
        last_at=exit_log.last_at,
        queue_depth=0,
        state=exit_monitor.state,
        last_tick_at=exit_monitor.last_tick_at,
        open_positions=exit_monitor.open_positions,
        blocked=exit_monitor.blocked,
    )


@router.get("/settlement/log", response_model=SectionLogResponse)
def get_settlement_log(
    limit: int = 200,
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> SectionLogResponse:
    # Settlement monitor mirrors the exit-monitor shape — position-driven, no
    # news queue. state surfaces whether the loop is running.
    return SectionLogResponse(
        entries=_entries_from_db(factory, SettlementDecisionRow, limit),
        counters=settlement_log.counters(),
        last_at=settlement_log.last_at,
        queue_depth=0,
        state=settlement_monitor.state,
    )
