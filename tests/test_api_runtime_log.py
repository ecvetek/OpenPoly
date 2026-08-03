"""Endpoint tests for /api/{embedding,analyzer,entry}/log (v7 / P4, EM4).

Pushes entries directly into the module-level log stores (no real
orchestrator processing) and verifies the response shape + counters +
orchestrator-state passthrough.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import openpoly.api.runtime_routes as runtime_routes
from openpoly.api.main import app, lifespan
from openpoly.db.engine import get_session_factory, init_db, make_engine, make_session_factory
from openpoly.db.tables import (
    AnalyzerCallRow,
    EmbeddingCallRow,
    EntryDecisionRow,
    ExitDecisionRow,
    SettlementDecisionRow,
)
from openpoly.news.manager import NewsSourceManager, manager
from openpoly.runtime import orchestrator as orch_module
from openpoly.runtime.exit_monitor import exit_monitor
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    ExitDecision,
    SettlementDecision,
    settlement_log,
    analyzer_log,
    embedding_log,
    entry_log,
    exit_log,
)


def _reset_exit_monitor_telemetry() -> None:
    # The lifespan test start()s the exit_monitor singleton, whose tick loop
    # fires once and sets last_tick_at — reset it so /api/exit/log heartbeat
    # assertions stay deterministic regardless of test order.
    exit_monitor._last_tick_at = None  # noqa: SLF001
    exit_monitor._last_tick_open = 0  # noqa: SLF001
    exit_monitor._last_tick_blocked = 0  # noqa: SLF001
    exit_monitor._last_tick = None  # noqa: SLF001
    exit_monitor._tick_events.clear()  # noqa: SLF001


@pytest.fixture(autouse=True)
def _reset_logs_and_orchestrator() -> Any:
    """Fresh log stores + orchestrator singleton per test."""
    embedding_log.reset()
    analyzer_log.reset()
    entry_log.reset()
    exit_log.reset()
    settlement_log.reset()
    orch_module._reset_singleton_for_tests()
    _reset_exit_monitor_telemetry()
    yield
    embedding_log.reset()
    analyzer_log.reset()
    entry_log.reset()
    exit_log.reset()
    settlement_log.reset()
    orch_module._reset_singleton_for_tests()
    _reset_exit_monitor_telemetry()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def db(tmp_path) -> Any:
    """A throwaway per-test DB, overriding the app's session-factory
    dependency — ``entries`` is now DB-sourced (durable), so tests that seed
    call-log rows need isolation from the shared session-wide DB, same as
    ``test_api_portfolio.py``'s ``env`` fixture."""
    engine = make_engine(f"sqlite:///{tmp_path / 'runtime_log.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    app.dependency_overrides[get_session_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_session_factory, None)
    engine.dispose()


# ---------- shape ----------


async def test_analyzer_log_empty_initially() -> None:
    client = await _client()
    try:
        r = await client.get("/api/analyzer/log")
    finally:
        await client.aclose()
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["counters"] == {"ok": 0, "skip": 0, "fail_open": 0, "error": 0}
    assert body["last_at"] is None
    assert body["queue_depth"] == 0
    assert body["state"] == "stopped"


async def test_entry_log_empty_initially() -> None:
    client = await _client()
    try:
        r = await client.get("/api/entry/log")
    finally:
        await client.aclose()
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["counters"] == {"ok": 0, "skip": 0, "fail_open": 0, "error": 0}


async def test_embedding_log_empty_initially() -> None:
    client = await _client()
    try:
        r = await client.get("/api/embedding/log")
    finally:
        await client.aclose()
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["counters"] == {"ok": 0, "skip": 0, "fail_open": 0, "error": 0}
    assert body["last_at"] is None
    assert body["queue_depth"] == 0
    assert body["state"] == "stopped"


async def test_embedding_log_returns_appended_entries(db) -> None:
    # Real production behavior: every call lands in *both* the ring
    # (counters/last_at) and the DB (durable entries) via the orchestrator's
    # persist hook — mirror both here.
    embedding_log.append(
        EmbeddingCall(
            ts=10.0,
            news_id="n1",
            news_content_preview="fed news",
            urgency="high",
            verdict="ok",
            candidate_count=2,
            top_market_id="m1",
            top_score=0.92,
            catalog_size=88,
            latency_ms=7,
        )
    )
    embedding_log.append(
        EmbeddingCall(
            ts=11.0,
            news_id="n2",
            news_content_preview="noise",
            urgency="low",
            verdict="skip",
            candidate_count=0,
            top_market_id=None,
            top_score=None,
            catalog_size=88,
            latency_ms=3,
        )
    )
    with db() as session:
        session.add_all(
            [
                EmbeddingCallRow(
                    ts=10.0,
                    news_id="n1",
                    news_content_preview="fed news",
                    urgency="high",
                    verdict="ok",
                    candidate_count=2,
                    top_market_id="m1",
                    top_score=0.92,
                    catalog_size=88,
                    latency_ms=7,
                    error=None,
                ),
                EmbeddingCallRow(
                    ts=11.0,
                    news_id="n2",
                    news_content_preview="noise",
                    urgency="low",
                    verdict="skip",
                    candidate_count=0,
                    top_market_id=None,
                    top_score=None,
                    catalog_size=88,
                    latency_ms=3,
                    error=None,
                ),
            ]
        )
        session.commit()
    client = await _client()
    try:
        r = await client.get("/api/embedding/log")
    finally:
        await client.aclose()
    body = r.json()
    assert [e["news_id"] for e in body["entries"]] == ["n1", "n2"]
    assert body["entries"][0]["top_market_id"] == "m1"
    assert body["entries"][0]["candidate_count"] == 2
    assert body["counters"]["ok"] == 1
    assert body["counters"]["skip"] == 1
    assert body["last_at"] == 11.0


async def test_embedding_log_includes_polymarket_url_when_catalogued(db) -> None:
    """top_market_id resolves against the live catalog into a real
    Polymarket link."""
    import json
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary

    embedding_log.append(
        EmbeddingCall(
            ts=10.0,
            news_id="n1",
            news_content_preview="fed news",
            urgency="high",
            verdict="ok",
            candidate_count=1,
            top_market_id="m1",
            top_score=0.9,
            catalog_size=10,
            latency_ms=5,
        )
    )
    with db() as session:
        session.add(
            EmbeddingCallRow(
                ts=10.0,
                news_id="n1",
                news_content_preview="fed news",
                urgency="high",
                verdict="ok",
                candidate_count=1,
                top_market_id="m1",
                top_score=0.9,
                catalog_size=10,
                latency_ms=5,
                error=None,
            )
        )
        session.commit()

    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Q?",
        "slug": "q-slug",
        "clobTokenIds": json.dumps(["yes-tok", "no-tok"]),
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E", "slug": "event-slug"})
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        msm.store = fresh
        client = await _client()
        try:
            r = await client.get("/api/embedding/log")
        finally:
            await client.aclose()
    finally:
        msm.store = saved_store
    assert (
        r.json()["entries"][0]["top_market_polymarket_url"]
        == "https://polymarket.com/event/event-slug"
    )


async def test_embedding_log_polymarket_url_null_when_not_catalogued(db) -> None:
    embedding_log.append(
        EmbeddingCall(
            ts=10.0,
            news_id="n1",
            news_content_preview="fed news",
            urgency="high",
            verdict="ok",
            candidate_count=1,
            top_market_id="m1",
            top_score=0.9,
            catalog_size=10,
            latency_ms=5,
        )
    )
    with db() as session:
        session.add(
            EmbeddingCallRow(
                ts=10.0,
                news_id="n1",
                news_content_preview="fed news",
                urgency="high",
                verdict="ok",
                candidate_count=1,
                top_market_id="m1",
                top_score=0.9,
                catalog_size=10,
                latency_ms=5,
                error=None,
            )
        )
        session.commit()
    client = await _client()
    try:
        r = await client.get("/api/embedding/log")
    finally:
        await client.aclose()
    assert r.json()["entries"][0]["top_market_polymarket_url"] is None


def _seed_analyzer_rows(db, *rows: AnalyzerCall) -> None:
    with db() as session:
        session.add_all([AnalyzerCallRow(**r.to_dict()) for r in rows])
        session.commit()


async def test_analyzer_log_returns_appended_entries(db) -> None:
    calls = [
        AnalyzerCall(
            ts=10.0,
            news_id="n1",
            news_content_preview="hello",
            urgency="high",
            verdict="ok",
            p_model=0.55,
            confidence="medium",
            market_id="m1",
            latency_ms=42,
        ),
        AnalyzerCall(
            ts=11.0,
            news_id="n2",
            news_content_preview="bye",
            urgency="low",
            verdict="skip",
            p_model=None,
            confidence=None,
            market_id=None,
            latency_ms=3,
        ),
    ]
    for c in calls:
        analyzer_log.append(c)
    _seed_analyzer_rows(db, *calls)

    client = await _client()
    try:
        r = await client.get("/api/analyzer/log")
    finally:
        await client.aclose()
    body = r.json()
    assert [e["news_id"] for e in body["entries"]] == ["n1", "n2"]
    assert body["counters"]["ok"] == 1
    assert body["counters"]["skip"] == 1
    assert body["last_at"] == 11.0


async def test_analyzer_log_includes_polymarket_url_when_catalogued(db) -> None:
    import json
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary

    call = AnalyzerCall(
        ts=10.0,
        news_id="n1",
        news_content_preview="hello",
        urgency="high",
        verdict="ok",
        p_model=0.55,
        confidence="medium",
        market_id="m1",
        latency_ms=42,
    )
    analyzer_log.append(call)
    _seed_analyzer_rows(db, call)

    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Q?",
        "slug": "q-slug",
        "clobTokenIds": json.dumps(["yes-tok", "no-tok"]),
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E", "slug": "event-slug"})
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        msm.store = fresh
        client = await _client()
        try:
            r = await client.get("/api/analyzer/log")
        finally:
            await client.aclose()
    finally:
        msm.store = saved_store
    assert (
        r.json()["entries"][0]["market_polymarket_url"]
        == "https://polymarket.com/event/event-slug"
    )


async def test_analyzer_log_polymarket_url_null_when_not_catalogued(db) -> None:
    call = AnalyzerCall(
        ts=10.0,
        news_id="n1",
        news_content_preview="hello",
        urgency="high",
        verdict="ok",
        p_model=0.55,
        confidence="medium",
        market_id="m1",
        latency_ms=42,
    )
    analyzer_log.append(call)
    _seed_analyzer_rows(db, call)
    client = await _client()
    try:
        r = await client.get("/api/analyzer/log")
    finally:
        await client.aclose()
    assert r.json()["entries"][0]["market_polymarket_url"] is None


async def test_analyzer_log_polymarket_url_null_when_no_market_id(db) -> None:
    """A skip verdict with market_id=None must not attempt a catalog lookup
    (and stays None)."""
    call = AnalyzerCall(
        ts=10.0,
        news_id="n2",
        news_content_preview="bye",
        urgency="low",
        verdict="skip",
        p_model=None,
        confidence=None,
        market_id=None,
        latency_ms=3,
    )
    analyzer_log.append(call)
    _seed_analyzer_rows(db, call)
    client = await _client()
    try:
        r = await client.get("/api/analyzer/log")
    finally:
        await client.aclose()
    assert r.json()["entries"][0]["market_polymarket_url"] is None


async def test_entry_log_returns_appended_entries(db) -> None:
    decision = EntryDecision(
        ts=10.0,
        news_id="n1",
        ar_p_model=0.6,
        ar_market_id="m1",
        verdict="ok",
        side="yes",
        qty=20.0,
        price=0.5,
        reason=None,
        latency_ms=15,
    )
    entry_log.append(decision)
    with db() as session:
        session.add(EntryDecisionRow(**decision.to_dict()))
        session.commit()
    client = await _client()
    try:
        r = await client.get("/api/entry/log")
    finally:
        await client.aclose()
    body = r.json()
    assert body["entries"][0]["news_id"] == "n1"
    assert body["entries"][0]["side"] == "yes"
    assert body["counters"]["ok"] == 1


# ---------- read-path split: entries is DB-sourced, counters/last_at is ring-sourced ----------


async def test_entries_and_counters_are_independently_sourced(db) -> None:
    """A push into the ring only (no DB row) must still update counters /
    last_at, but must NOT appear in entries — proving entries reads the DB,
    not the ring, while counters/last_at still read the ring. This is the
    split the read-path refactor depends on."""
    analyzer_log.append(
        AnalyzerCall(
            ts=10.0,
            news_id="ring-only",
            news_content_preview="x",
            urgency="high",
            verdict="ok",
            p_model=0.5,
            confidence="medium",
            market_id="m",
            latency_ms=1,
        )
    )
    client = await _client()
    try:
        r = await client.get("/api/analyzer/log")
    finally:
        await client.aclose()
    body = r.json()
    assert body["entries"] == []  # DB is empty — ring push alone doesn't appear here
    assert body["counters"]["ok"] == 1  # but counters/last_at still reflect the ring
    assert body["last_at"] == 10.0


# ---------- limit query ----------


async def test_analyzer_log_limit(db) -> None:
    calls = [
        AnalyzerCall(
            ts=float(i),
            news_id=f"n{i}",
            news_content_preview="x",
            urgency="high",
            verdict="ok",
            p_model=0.5,
            confidence="medium",
            market_id="m",
            latency_ms=1,
        )
        for i in range(5)
    ]
    for c in calls:
        analyzer_log.append(c)
    _seed_analyzer_rows(db, *calls)
    client = await _client()
    try:
        r = await client.get("/api/analyzer/log", params={"limit": 2})
    finally:
        await client.aclose()
    body = r.json()
    assert [e["news_id"] for e in body["entries"]] == ["n3", "n4"]
    # counters still reflect the full ring, not the limited slice
    assert body["counters"]["ok"] == 5


async def test_analyzer_log_limit_clamped_to_max(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absurd ?limit= must not force an unbounded query — previously
    these 5 routes had no upper clamp at all, unlike portfolio_routes.py /
    inspect_routes.py's capped ``limit`` handling. Lower the max via
    monkeypatch instead of seeding 500+ rows, for a fast, deterministic
    assertion of the same clamping behavior."""
    monkeypatch.setattr(runtime_routes, "SECTION_LOG_LIMIT_MAX", 2)
    calls = [
        AnalyzerCall(
            ts=float(i),
            news_id=f"n{i}",
            news_content_preview="x",
            urgency="high",
            verdict="ok",
            p_model=0.5,
            confidence="medium",
            market_id="m",
            latency_ms=1,
        )
        for i in range(5)
    ]
    _seed_analyzer_rows(db, *calls)
    client = await _client()
    try:
        r = await client.get("/api/analyzer/log", params={"limit": 999_999_999})
    finally:
        await client.aclose()
    body = r.json()
    assert r.status_code == 200
    assert len(body["entries"]) == 2  # clamped to the (patched) max, not 5


# ---------- lifespan wiring ----------


async def test_lifespan_starts_and_stops_orchestrator() -> None:
    """Driving the lifespan context manager runs startup + shutdown. After
    startup the orchestrator is running and the manager has its pipeline
    hook installed; after shutdown both are quiesced."""

    # Fresh singleton + clean manager (reset for this test).
    orch_module._reset_singleton_for_tests()
    NewsSourceManager.__init__(manager)

    assert manager._pipeline_hook is None  # noqa: SLF001 — internal field probe

    async with lifespan(app):
        orch = orch_module.get_orchestrator()
        assert orch.state == "running"
        assert manager._pipeline_hook is not None  # noqa: SLF001
        # The hook is the orchestrator's enqueue method.
        assert manager._pipeline_hook == orch.enqueue  # noqa: SLF001

    # After shutdown, both are reset.
    orch = orch_module.get_orchestrator()
    assert orch.state == "stopped"
    assert manager._pipeline_hook is None  # noqa: SLF001


# ---------- queue_depth passthrough ----------


async def test_endpoint_reflects_orchestrator_queue_depth() -> None:
    from openpoly.news.ring_buffer import NewsItem

    orch = orch_module.get_orchestrator()
    # Don't start the orchestrator → enqueued items pile up.
    item = NewsItem(
        id="n1",
        content="x",
        urgency="high",
        sentiment=None,
        published_at=0.0,
        received_at=0.0,
    )
    orch.enqueue(item)
    orch.enqueue(item)

    client = await _client()
    try:
        r = await client.get("/api/analyzer/log")
    finally:
        await client.aclose()
    body = r.json()
    assert body["queue_depth"] == 2
    assert body["state"] == "stopped"


# ---------- exit log ----------


async def test_exit_log_empty_initially() -> None:
    client = await _client()
    try:
        r = await client.get("/api/exit/log")
    finally:
        await client.aclose()
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["counters"] == {"ok": 0, "skip": 0, "fail_open": 0, "error": 0}
    assert body["last_at"] is None
    # The exit monitor is position-driven, not a news queue.
    assert body["queue_depth"] == 0
    # state mirrors the (un-started) monitor singleton; tick heartbeat is unset.
    assert body["state"] == "stopped"
    assert body["last_tick_at"] is None
    assert body["open_positions"] == 0
    assert body["blocked"] == 0


async def test_exit_log_returns_appended_entries(db) -> None:
    decisions = [
        ExitDecision(
            ts=10.0,
            position_id=1,
            market_id="m1",
            side="yes",
            verdict="ok",
            trigger="take_profit",
            return_pct=0.25,
            fill_price=0.62,
            realized_pnl=2.1,
            reason="take_profit",
        ),
        ExitDecision(
            ts=11.0,
            position_id=2,
            market_id="m2",
            side="no",
            verdict="skip",
            trigger=None,
            return_pct=0.03,
            fill_price=None,
            realized_pnl=None,
            reason="within thresholds",
        ),
    ]
    for d in decisions:
        exit_log.append(d)
    with db() as session:
        session.add_all([ExitDecisionRow(**d.to_dict()) for d in decisions])
        session.commit()
    client = await _client()
    try:
        r = await client.get("/api/exit/log")
    finally:
        await client.aclose()
    body = r.json()
    assert [e["position_id"] for e in body["entries"]] == [1, 2]
    assert body["entries"][0]["trigger"] == "take_profit"
    assert body["entries"][0]["realized_pnl"] == 2.1
    assert body["counters"]["ok"] == 1
    assert body["counters"]["skip"] == 1
    assert body["state"] == "stopped"


# ---------- settlement log ----------


async def test_settlement_log_empty_initially() -> None:
    client = await _client()
    try:
        r = await client.get("/api/settlement/log")
    finally:
        await client.aclose()
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["counters"] == {"ok": 0, "skip": 0, "fail_open": 0, "error": 0}
    assert body["last_at"] is None
    assert body["queue_depth"] == 0
    # state comes from the singleton settlement_monitor — stopped in tests.
    assert body["state"] == "stopped"


async def test_settlement_log_returns_appended_entries(db) -> None:
    decisions = [
        SettlementDecision(
            ts=10.0,
            position_id=1,
            market_id="m1",
            side="yes",
            verdict="ok",
            final_price=1.0,
            realized_pnl=6.0,
            reason="settlement",
            error=None,
        ),
        SettlementDecision(
            ts=11.0,
            position_id=2,
            market_id="m2",
            side="no",
            verdict="skip",
            final_price=None,
            realized_pnl=None,
            reason="no_outcome_prices",
            error=None,
        ),
    ]
    for d in decisions:
        settlement_log.append(d)
    with db() as session:
        session.add_all([SettlementDecisionRow(**d.to_dict()) for d in decisions])
        session.commit()
    client = await _client()
    try:
        r = await client.get("/api/settlement/log")
    finally:
        await client.aclose()
    body = r.json()
    assert [e["position_id"] for e in body["entries"]] == [1, 2]
    assert body["entries"][0]["final_price"] == 1.0
    assert body["entries"][0]["realized_pnl"] == 6.0
    assert body["entries"][1]["reason"] == "no_outcome_prices"
    assert body["counters"]["ok"] == 1
    assert body["counters"]["skip"] == 1
