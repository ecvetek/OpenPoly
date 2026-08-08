"""Endpoint tests for /api/positions and /api/fills (PF5)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from openpoly.api.main import app
from openpoly.api.portfolio_routes import get_portfolio_store
from openpoly.db.engine import get_session_factory, init_db, make_engine, make_session_factory
from openpoly.db.tables import (
    AnalyzerCallRow,
    EntryDecisionRow,
    ExitDecisionRow,
    NewsItemRow,
)
from openpoly.portfolio import PortfolioStore


@pytest.fixture
def env(tmp_path):
    """A PortfolioStore + session factory on the same throwaway DB, both
    wired into the app via dependency override, plus a TestClient.
    ``analyzer_decisions`` is DB-sourced (the ``AnalyzerCallRow`` table), so
    the injected ``get_session_factory`` dependency must point at the same
    engine as ``store`` or the two would see different databases."""
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    factory = make_session_factory(engine)
    store = PortfolioStore(factory)
    app.dependency_overrides[get_portfolio_store] = lambda: store
    app.dependency_overrides[get_session_factory] = lambda: factory
    yield store, TestClient(app), factory
    app.dependency_overrides.clear()
    engine.dispose()


def _open(store: PortfolioStore, market_id: str, side: str, token_id: str, ts: float = 100.0):
    return store.open_position(
        market_id=market_id,
        side=side,
        token_id=token_id,
        condition_id=f"0x{market_id}",
        price=0.42,
        qty=20.0,
        ts=ts,
        news_id="n1",
    )


def test_positions_empty(env) -> None:
    _store, client, _factory = env
    assert client.get("/api/positions").json() == {"positions": []}


def test_fills_empty(env) -> None:
    _store, client, _factory = env
    assert client.get("/api/fills").json() == {"fills": []}


def test_positions_returns_open_and_closed(env) -> None:
    store, client, _factory = env
    _open(store, "m1", "yes", "ty1", ts=100.0)
    h2 = _open(store, "m2", "no", "tn2", ts=101.0)
    store.close_position(
        h2.position_id,
        sell_price=0.50,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )

    positions = client.get("/api/positions").json()["positions"]
    assert len(positions) == 2
    # Newest first — m2 opened last.
    assert positions[0]["market_id"] == "m2"
    assert positions[0]["status"] == "closed"
    assert positions[0]["close_reason"] == "take_profit"
    assert positions[0]["realized_pnl"] is not None
    assert positions[1]["market_id"] == "m1"
    assert positions[1]["status"] == "open"
    assert positions[1]["realized_pnl"] is None


def test_fills_returns_ledger(env) -> None:
    store, client, _factory = env
    h1 = _open(store, "m1", "yes", "ty1")
    store.close_position(
        h1.position_id,
        sell_price=0.50,
        ts=200.0,
        close_reason="stop_loss",
        trigger="stop_loss",
    )

    fills = client.get("/api/fills").json()["fills"]
    assert len(fills) == 2
    # Newest first — the sell.
    assert fills[0]["action"] == "sell"
    assert fills[0]["trigger"] == "stop_loss"
    assert fills[1]["action"] == "buy"
    assert fills[1]["news_id"] == "n1"


def test_limit_param_honored(env) -> None:
    store, client, _factory = env
    for i in range(5):
        _open(store, f"m{i}", "yes", f"ty{i}", ts=float(i))
    assert len(client.get("/api/positions?limit=2").json()["positions"]) == 2
    assert len(client.get("/api/fills?limit=1").json()["fills"]) == 1


# ---------- /api/portfolio/equity ----------


def test_equity_endpoint_empty(env) -> None:
    _store, client, _factory = env
    body = client.get("/api/portfolio/equity").json()
    assert body == {
        "points": [],
        "summary": {
            "realized": 0.0,
            "unrealized": 0.0,
            "total": 0.0,
            "open_positions": 0,
        },
    }


def test_equity_endpoint_with_position(env) -> None:
    store, client, _factory = env
    store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    body = client.get("/api/portfolio/equity").json()
    assert body["summary"]["open_positions"] == 1
    assert len(body["points"]) >= 1
    assert set(body["points"][0].keys()) == {
        "ts",
        "equity",
        "realized",
        "unrealized",
    }


def test_equity_endpoint_summary_alltime_but_points_windowed(env) -> None:
    """A position closed well outside the chart window must still count
    toward the all-time summary, even though it produces no chart points —
    proves the summary is a separate all-time path, not derived from the
    windowed curve's last point (which would silently drop it)."""
    import time

    from openpoly.api.portfolio_routes import (
        EQUITY_WINDOW_DEFAULT_HOURS,
        EQUITY_WINDOW_OPTIONS_HOURS,
    )

    store, client, _factory = env
    old_ts = time.time() - EQUITY_WINDOW_OPTIONS_HOURS[EQUITY_WINDOW_DEFAULT_HOURS] * 3
    h = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=old_ts,
        news_id="n1",
    )
    store.close_position(h.position_id, sell_price=0.55, ts=old_ts + 10, close_reason="take_profit")
    body = client.get("/api/portfolio/equity").json()
    assert body["summary"]["realized"] == pytest.approx(1.50)
    assert body["points"] == []


def test_equity_endpoint_window_hours_param_widens_the_chart(env) -> None:
    """A position closed ~3 days ago falls outside the default 24-hour
    window (no points) but inside a 7-day (168h) window (has points) —
    proves window_hours actually reaches build_equity_curve, not just the
    default."""
    import time

    store, client, _factory = env
    old_ts = time.time() - 3 * 86_400
    h = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=old_ts,
        news_id="n1",
    )
    store.close_position(h.position_id, sell_price=0.55, ts=old_ts + 10, close_reason="take_profit")
    assert client.get("/api/portfolio/equity?window_hours=24").json()["points"] == []
    assert client.get("/api/portfolio/equity?window_hours=168").json()["points"] != []


def test_equity_endpoint_unrecognized_window_hours_falls_back_to_default(env) -> None:
    """An out-of-safelist window_hours must not be used verbatim as an
    unbounded window — a value like 999 would defeat the whole point of
    windowing if it reached build_equity_curve as 999 hours."""
    import time

    store, client, _factory = env
    old_ts = time.time() - 3 * 86_400
    h = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=old_ts,
        news_id="n1",
    )
    store.close_position(h.position_id, sell_price=0.55, ts=old_ts + 10, close_reason="take_profit")
    # 999 isn't in the safelist -> falls back to the 24-hour default, so this
    # position (closed 3 days ago) still produces no points, same as the
    # default's own behavior in the sibling test above.
    assert client.get("/api/portfolio/equity?window_hours=999").json()["points"] == []


# ---------- /api/positions/{id} ----------


def test_get_position_by_id(env) -> None:
    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1", ts=100.0)
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["id"] == h.position_id
    assert body["market_id"] == "m1"
    assert body["token_id"] == "ty1"
    assert body["status"] == "open"
    assert body["avg_entry_price"] == 0.42


def test_get_position_by_id_404(env) -> None:
    _store, client, _factory = env
    assert client.get("/api/positions/99999").status_code == 404


# ---------- PD2: market_question lookup ----------


def test_get_position_includes_market_question_when_catalogued(env) -> None:
    """Position's condition_id resolves via MarketStore.get_by_condition →
    response carries the human question text."""
    import json
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")  # _open uses condition_id="0xm1"
    # Populate catalog with a market whose conditionId matches.
    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will the U.S. invade Iran before 2027?",
        "slug": "iran-2027",
        "clobTokenIds": json.dumps(["yes-tok", "no-tok"]),
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E"})
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        msm.store = fresh
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["market_question"] == "Will the U.S. invade Iran before 2027?"


def test_get_position_market_question_null_when_not_catalogued(env) -> None:
    """Market evicted / never catalogued → market_question is None (UI
    falls back to condition_id truncation)."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    saved_store = msm.store
    try:
        msm.store = MarketStore()  # empty catalog
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["market_question"] is None


def test_get_position_includes_market_end_date_when_catalogued(env) -> None:
    """Position's condition_id resolves via MarketStore.get_by_condition →
    response carries the market's resolution date as epoch seconds."""
    import json
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")  # _open uses condition_id="0xm1"
    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will the U.S. invade Iran before 2027?",
        "slug": "iran-2027",
        "clobTokenIds": json.dumps(["yes-tok", "no-tok"]),
        "endDate": "2027-01-01T00:00:00Z",
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E"})
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        msm.store = fresh
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["market_end_date"] == market.end_date.timestamp()


def test_get_position_market_end_date_null_when_not_catalogued(env) -> None:
    """Market evicted / never catalogued → market_end_date is None, same
    fallback semantics as market_question."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    saved_store = msm.store
    try:
        msm.store = MarketStore()  # empty catalog
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["market_end_date"] is None


def test_get_position_includes_market_tags_and_stats_when_catalogued(env) -> None:
    """Event tags + volume/liquidity/fee ride the same catalog lookup as
    market_question — populated together, from the same normalized Market."""
    import json
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will the U.S. invade Iran before 2027?",
        "slug": "iran-2027",
        "clobTokenIds": json.dumps(["yes-tok", "no-tok"]),
        "volume24hr": 12345.0,
        "liquidityNum": 6789.0,
        "feeSchedule": {"rate": 0.04},
    }
    market = normalize_gamma_market(
        raw,
        event={"id": "e", "title": "E", "tags": [{"slug": "crypto"}, {"slug": "fed-rates"}]},
    )
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        msm.store = fresh
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["market_tags"] == ["crypto", "fed-rates"]
    assert body["market_volume_24h"] == 12345.0
    assert body["market_liquidity"] == 6789.0
    assert body["market_taker_fee_rate"] == 0.04


def test_get_position_market_tags_null_when_not_catalogued(env) -> None:
    """Market evicted / never catalogued → market_tags and stats are None,
    same fallback semantics as market_question."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    saved_store = msm.store
    try:
        msm.store = MarketStore()  # empty catalog
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["market_tags"] is None
    assert body["market_volume_24h"] is None
    assert body["market_liquidity"] is None
    assert body["market_taker_fee_rate"] is None


def test_get_position_market_question_from_persisted_catalog_when_evicted(env) -> None:
    """Market left the live catalog but was captured by an earlier poll
    (persisted to market_catalog) → market_question/polymarket_url still
    resolve via that fallback; market_tags/stats stay None (not persisted)."""
    from openpoly.db.market_catalog_store import upsert_market_catalog_row
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore

    store, client, factory = env
    h = _open(store, "m1", "yes", "ty1")  # _open uses condition_id="0xm1"
    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will the U.S. invade Iran before 2027?",
        "slug": "iran-2027",
        "clobTokenIds": '["yes-tok", "no-tok"]',
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E"})
    with factory() as session:
        upsert_market_catalog_row(session, market, now=1.0)
        session.commit()

    saved_store = msm.store
    try:
        msm.store = MarketStore()  # evicted from the live catalog
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["market_question"] == "Will the U.S. invade Iran before 2027?"
    assert body["polymarket_url"] == "https://polymarket.com/event/iran-2027"
    assert body["market_end_date"] is None
    assert body["market_tags"] is None


def test_list_positions_market_question_from_persisted_catalog_when_evicted(env) -> None:
    from openpoly.db.market_catalog_store import upsert_market_catalog_row
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore

    store, client, factory = env
    _open(store, "m1", "yes", "ty1")
    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will the U.S. invade Iran before 2027?",
        "slug": "iran-2027",
        "clobTokenIds": '["yes-tok", "no-tok"]',
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E"})
    with factory() as session:
        upsert_market_catalog_row(session, market, now=1.0)
        session.commit()

    saved_store = msm.store
    try:
        msm.store = MarketStore()  # evicted from the live catalog
        body = client.get("/api/positions").json()
    finally:
        msm.store = saved_store
    assert body["positions"][0]["market_question"] == "Will the U.S. invade Iran before 2027?"
    assert body["positions"][0]["polymarket_url"] == "https://polymarket.com/event/iran-2027"


# ---------- PD3: analyzer_decisions lookup ----------


def test_get_position_includes_analyzer_decisions_when_log_has_match(env) -> None:
    """The persisted analyzer_call table carries one or more verdict=ok rows
    for our news_id → response surfaces them newest-first with the
    flattened shape."""
    from openpoly.runtime.section_log import AnalyzerCall

    store, client, factory = env
    h = _open(store, "m1", "yes", "ty1")  # _open uses news_id="n1"
    # Two ok calls + one unrelated + one error (latter two must be filtered out).
    calls = [
        AnalyzerCall(
            ts=100.0,
            news_id="n1",
            news_content_preview="x",
            urgency="high",
            verdict="ok",
            p_model=0.55,
            confidence="medium",
            market_id="m1",
            latency_ms=20,
            rationale="first attempt rationale",
        ),
        AnalyzerCall(
            ts=101.0,
            news_id="n1",
            news_content_preview="x",
            urgency="high",
            verdict="ok",
            p_model=0.60,
            confidence="high",
            market_id="m1",
            latency_ms=18,
            rationale="second attempt rationale",
        ),
        AnalyzerCall(
            ts=102.0,
            news_id="other-news",
            news_content_preview="y",
            urgency="high",
            verdict="ok",
            p_model=0.7,
            confidence="high",
            market_id="m9",
            latency_ms=15,
            rationale="unrelated news — must not appear",
        ),
        AnalyzerCall(
            ts=103.0,
            news_id="n1",
            news_content_preview="x",
            urgency="high",
            verdict="error",
            p_model=None,
            confidence=None,
            market_id=None,
            latency_ms=2,
            error="LLM down",
            rationale=None,
        ),
    ]
    with factory() as session:
        session.add_all([AnalyzerCallRow(**c.to_dict()) for c in calls])
        session.commit()
    body = client.get(f"/api/positions/{h.position_id}").json()

    decisions = body["analyzer_decisions"]
    assert isinstance(decisions, list)
    assert len(decisions) == 2  # error + unrelated filtered out
    # newest-first
    assert decisions[0]["rationale"] == "second attempt rationale"
    assert decisions[1]["rationale"] == "first attempt rationale"
    assert decisions[0]["p_model"] == 0.60
    assert decisions[0]["confidence"] == "high"
    assert decisions[0]["ts"] == 101.0
    # No noise fields
    assert set(decisions[0].keys()) == {"rationale", "self_check", "p_model", "confidence", "ts"}


def test_get_position_analyzer_decisions_empty_when_no_match(env) -> None:
    """No analyzer_call row for this position's news_id → empty list
    (UI shows 'rationale unavailable'). The fresh throwaway DB has no rows
    at all, so this is the natural default — no seeding needed."""
    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["analyzer_decisions"] == []


def test_get_position_analyzer_decisions_survives_ring_eviction(env) -> None:
    """The direct regression test for the reported bug: a position whose
    originating analyzer call is long gone from the in-memory ring (>200
    newer calls since) must still resolve its rationale, because the lookup
    is now DB-backed rather than a ring scan."""
    from openpoly.runtime.section_log import AnalyzerCall

    store, client, factory = env
    h = _open(store, "m1", "yes", "ty1")  # _open uses news_id="n1"
    rows = [
        AnalyzerCallRow(
            **AnalyzerCall(
                ts=100.0,
                news_id="n1",
                news_content_preview="x",
                urgency="high",
                verdict="ok",
                p_model=0.55,
                confidence="medium",
                market_id="m1",
                latency_ms=20,
                rationale="the original, long-evicted rationale",
            ).to_dict()
        )
    ]
    # 250 unrelated rows — comfortably more than the in-memory ring's 200-slot
    # capacity, simulating everything that would have evicted the original
    # call from analyzer_log by the time this position is later inspected.
    rows.extend(
        AnalyzerCallRow(
            **AnalyzerCall(
                ts=200.0 + i,
                news_id=f"later-{i}",
                news_content_preview="y",
                urgency="high",
                verdict="ok",
                p_model=0.5,
                confidence="medium",
                market_id="m2",
                latency_ms=10,
                rationale="noise",
            ).to_dict()
        )
        for i in range(250)
    )
    with factory() as session:
        session.add_all(rows)
        session.commit()

    body = client.get(f"/api/positions/{h.position_id}").json()
    decisions = body["analyzer_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["rationale"] == "the original, long-evicted rationale"


def test_get_position_analyzer_decisions_empty_when_news_id_null(env) -> None:
    """Paper / manual positions may have news_id=None — lookup must skip
    cleanly without scanning the ring."""
    store, client, _factory = env
    # Create position with news_id=None explicitly.
    h = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xm1",
        price=0.42,
        qty=10.0,
        ts=100.0,
        news_id=None,
    )
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["analyzer_decisions"] == []


# ---------- v15 PR1: list endpoint augments each row with question + decisions ----------


def test_list_positions_includes_market_question_and_analyzer_decisions(env) -> None:
    """list endpoint must surface market_question + analyzer_decisions per row
    (same shape as /positions/{id}). Card-style UI relies on these being
    available list-wide so it can render question / rationale without
    fanning out to /positions/{id} per row."""
    import json
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary
    from openpoly.runtime.section_log import AnalyzerCall

    store, client, factory = env
    _open(store, "m1", "yes", "ty1", ts=100.0)
    _open(store, "m2", "no", "tn2", ts=101.0)  # no catalog entry — must be None

    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will the U.S. invade Iran before 2027?",
        "slug": "iran-2027",
        "clobTokenIds": json.dumps(["yes-tok", "no-tok"]),
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E"})

    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        msm.store = fresh
        call = AnalyzerCall(
            ts=99.0,
            news_id="n1",
            news_content_preview="x",
            urgency="high",
            verdict="ok",
            p_model=0.55,
            confidence="medium",
            market_id="m1",
            latency_ms=20,
            rationale="opened m1 because reasons",
        )
        with factory() as session:
            session.add(AnalyzerCallRow(**call.to_dict()))
            session.commit()
        positions = client.get("/api/positions").json()["positions"]
    finally:
        msm.store = saved_store

    assert len(positions) == 2
    # newest-first: m2 opened at ts=101
    by_market = {p["market_id"]: p for p in positions}

    # m1: catalogued + analyzer match → both populated
    assert by_market["m1"]["market_question"] == "Will the U.S. invade Iran before 2027?"
    decisions = by_market["m1"]["analyzer_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["rationale"] == "opened m1 because reasons"
    assert set(decisions[0].keys()) == {"rationale", "self_check", "p_model", "confidence", "ts"}

    # m2: not in catalog + no analyzer match for its news_id (n1 is m1's; m2 also
    # uses news_id="n1" per _open helper, but the rationale text says "m1" — the
    # filter is by news_id, not market_id, so m2 will *also* see the same call).
    # This is correct backend behavior — the helper does not gate on market.
    assert by_market["m2"]["market_question"] is None
    # m2's news_id is also "n1" (per _open default), so it sees the same call.
    assert len(by_market["m2"]["analyzer_decisions"]) == 1


def test_list_positions_analyzer_decisions_grouped_correctly_per_news_id(env) -> None:
    """Regression test for the N+1 -> bulk-query rewrite: positions with
    DIFFERENT news_ids must each get only their own matching decisions, and
    a position with no news linkage at all must get an empty list — proving
    the bulk lookup's grouping-by-news_id is correct, not just incidentally
    right because a single shared news_id (this file's _open() default)
    makes every position look the same."""
    from openpoly.runtime.section_log import AnalyzerCall

    store, client, factory = env
    h1 = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xm1",
        price=0.42,
        qty=20.0,
        ts=100.0,
        news_id="news-a",
    )
    h2 = store.open_position(
        market_id="m2",
        side="yes",
        token_id="ty2",
        condition_id="0xm2",
        price=0.42,
        qty=20.0,
        ts=101.0,
        news_id="news-b",
    )
    h3 = store.open_position(
        market_id="m3",
        side="yes",
        token_id="ty3",
        condition_id="0xm3",
        price=0.42,
        qty=20.0,
        ts=102.0,
        news_id=None,
    )

    with factory() as session:
        session.add_all(
            [
                AnalyzerCallRow(
                    **AnalyzerCall(
                        ts=90.0,
                        news_id="news-a",
                        news_content_preview="x",
                        urgency="high",
                        verdict="ok",
                        p_model=0.6,
                        confidence="medium",
                        market_id="m1",
                        latency_ms=10,
                        rationale="rationale for news-a",
                    ).to_dict()
                ),
                AnalyzerCallRow(
                    **AnalyzerCall(
                        ts=91.0,
                        news_id="news-b",
                        news_content_preview="y",
                        urgency="high",
                        verdict="ok",
                        p_model=0.5,
                        confidence="medium",
                        market_id="m2",
                        latency_ms=10,
                        rationale="rationale for news-b",
                    ).to_dict()
                ),
            ]
        )
        session.commit()

    positions = client.get("/api/positions").json()["positions"]
    by_id = {p["id"]: p for p in positions}

    assert [d["rationale"] for d in by_id[h1.position_id]["analyzer_decisions"]] == [
        "rationale for news-a"
    ]
    assert [d["rationale"] for d in by_id[h2.position_id]["analyzer_decisions"]] == [
        "rationale for news-b"
    ]
    assert by_id[h3.position_id]["analyzer_decisions"] == []


def test_list_positions_market_question_null_when_no_catalog(env) -> None:
    """Empty catalog → every row has market_question=None (UI must still
    render — card header falls back to condition_id truncation)."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    _open(store, "m1", "yes", "ty1", ts=100.0)
    _open(store, "m2", "no", "tn2", ts=101.0)

    saved_store = msm.store
    try:
        msm.store = MarketStore()  # empty
        positions = client.get("/api/positions").json()["positions"]
    finally:
        msm.store = saved_store

    assert len(positions) == 2
    for p in positions:
        assert p["market_question"] is None
        # analyzer_decisions still present (empty list when no log match)
        assert isinstance(p["analyzer_decisions"], list)


# ---------- polymarket_url ----------


def test_get_position_includes_polymarket_url_when_catalogued(env) -> None:
    """market_question and polymarket_url share the same catalog lookup —
    both populated together. No event slug supplied here, so the url falls
    back to the market's own slug."""
    import json
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will the U.S. invade Iran before 2027?",
        "slug": "iran-2027",
        "clobTokenIds": json.dumps(["yes-tok", "no-tok"]),
    }
    market = normalize_gamma_market(raw, event={"id": "e", "title": "E"})
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
        msm.store = fresh
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["polymarket_url"] == "https://polymarket.com/event/iran-2027"


def _book(token_id: str, *, bids: list[tuple[float, float]]):
    from openpoly.markets.models import OrderBook

    return OrderBook(token_id=token_id, ts=100.0, bids=bids, asks=[])


def test_get_position_unrealized_pnl_when_open_with_book_gain(env) -> None:
    """Open position, live book has a better bid than entry → positive
    mark-to-market P&L, marked at the level-1 bid (not mid/ask)."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")  # avg_entry_price=0.42, qty=20.0
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.set_order_books([_book("ty1", bids=[(0.50, 100.0), (0.49, 50.0)])])
        msm.store = fresh
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["unrealized_pnl"] == pytest.approx((0.50 - 0.42) * 20.0)


def test_get_position_unrealized_pnl_when_open_with_book_loss(env) -> None:
    """Bid below entry → negative unrealized P&L."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.set_order_books([_book("ty1", bids=[(0.30, 100.0)])])
        msm.store = fresh
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["unrealized_pnl"] == pytest.approx((0.30 - 0.42) * 20.0)


def test_get_position_unrealized_pnl_null_when_no_book(env) -> None:
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    saved_store = msm.store
    try:
        msm.store = MarketStore()  # no order books at all
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["unrealized_pnl"] is None


def test_get_position_unrealized_pnl_null_when_closed(env) -> None:
    """Even if a live book exists, a closed position reports None — use
    realized_pnl instead."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    store.close_position(
        h.position_id,
        sell_price=0.55,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.set_order_books([_book("ty1", bids=[(0.55, 100.0)])])
        msm.store = fresh
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["unrealized_pnl"] is None
    assert body["realized_pnl"] is not None


def test_list_positions_includes_unrealized_pnl(env) -> None:
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    _open(store, "m1", "yes", "ty1", ts=100.0)
    saved_store = msm.store
    try:
        fresh = MarketStore()
        fresh.set_order_books([_book("ty1", bids=[(0.50, 100.0)])])
        msm.store = fresh
        positions = client.get("/api/positions").json()["positions"]
    finally:
        msm.store = saved_store
    assert positions[0]["unrealized_pnl"] == pytest.approx((0.50 - 0.42) * 20.0)


def test_get_position_polymarket_url_null_when_not_catalogued(env) -> None:
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    saved_store = msm.store
    try:
        msm.store = MarketStore()  # empty catalog
        body = client.get(f"/api/positions/{h.position_id}").json()
    finally:
        msm.store = saved_store
    assert body["polymarket_url"] is None


# ---------- news_id + news summary ----------


def test_get_position_includes_news_id(env) -> None:
    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")  # _open uses news_id="n1"
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["news_id"] == "n1"


def test_get_position_news_id_null_when_no_linkage(env) -> None:
    store, client, _factory = env
    h = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xm1",
        price=0.42,
        qty=10.0,
        ts=100.0,
        news_id=None,
    )
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["news_id"] is None
    assert body["news"] is None


def test_get_position_includes_news_summary_when_persisted(env) -> None:
    """The triggering NewsItemRow is persisted → response inlines its full
    content/urgency/sentiment/published_at, not just the news_id."""
    store, client, factory = env
    h = _open(store, "m1", "yes", "ty1")  # _open uses news_id="n1"
    with factory() as session:
        session.add(
            NewsItemRow(
                news_id="n1",
                content="Some breaking headline about m1's market.",
                urgency="high",
                sentiment="negative",
                published_at=90.0,
                received_at=91.0,
            )
        )
        session.commit()
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["news"] == {
        "content": "Some breaking headline about m1's market.",
        "urgency": "high",
        "sentiment": "negative",
        "published_at": 90.0,
    }


def test_get_position_news_null_when_not_persisted(env) -> None:
    """news_id exists (via the fill) but no matching NewsItemRow was ever
    persisted — news must be None, not a crash."""
    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["news_id"] == "n1"
    assert body["news"] is None


# ---------- exit_decision ----------


def test_get_position_exit_decision_null_when_open(env) -> None:
    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["exit_decision"] is None


def test_get_position_includes_exit_decision_when_closed(env) -> None:
    """A persisted verdict=ok ExitDecision for this position_id surfaces its
    trigger/return_pct/peak_price/reason — richer than the coarse
    close_reason enum alone."""
    from openpoly.runtime.section_log import ExitDecision

    store, client, factory = env
    h = _open(store, "m1", "yes", "ty1")
    store.close_position(
        h.position_id,
        sell_price=0.30,
        ts=200.0,
        close_reason="stop_loss",
        trigger="stop_loss",
    )
    decision = ExitDecision(
        ts=200.0,
        position_id=h.position_id,
        market_id="m1",
        side="yes",
        verdict="ok",
        trigger="stop_loss",
        return_pct=-0.15,
        fill_price=0.30,
        realized_pnl=-2.4,
        reason="stop_loss threshold breached",
        peak_price=0.48,
    )
    with factory() as session:
        session.add(ExitDecisionRow(**decision.to_dict()))
        session.commit()
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["exit_decision"] == {
        "trigger": "stop_loss",
        "return_pct": -0.15,
        "fill_price": 0.30,
        "realized_pnl": -2.4,
        "reason": "stop_loss threshold breached",
        "peak_price": 0.48,
        "ts": 200.0,
    }


def test_get_position_exit_decision_ignores_other_positions_and_skips(env) -> None:
    """Filters by this exact position_id and verdict=ok — a skip/error row
    for the same position, or an ok row for a *different* position, must
    not leak in."""
    from openpoly.runtime.section_log import ExitDecision

    store, client, factory = env
    h1 = _open(store, "m1", "yes", "ty1", ts=100.0)
    h2 = _open(store, "m2", "no", "tn2", ts=101.0)
    rows = [
        ExitDecisionRow(
            **ExitDecision(
                ts=150.0,
                position_id=h1.position_id,
                market_id="m1",
                side="yes",
                verdict="skip",
                trigger=None,
                return_pct=0.01,
                fill_price=None,
                realized_pnl=None,
                reason="within threshold",
            ).to_dict()
        ),
        ExitDecisionRow(
            **ExitDecision(
                ts=200.0,
                position_id=h2.position_id,
                market_id="m2",
                side="no",
                verdict="ok",
                trigger="take_profit",
                return_pct=0.2,
                fill_price=0.6,
                realized_pnl=3.0,
                reason="tp hit",
            ).to_dict()
        ),
    ]
    with factory() as session:
        session.add_all(rows)
        session.commit()
    body = client.get(f"/api/positions/{h1.position_id}").json()
    assert body["exit_decision"] is None


# ---------- entry_decision lookup ----------


def test_get_position_entry_decision_null_when_not_persisted(env) -> None:
    store, client, _factory = env
    h = _open(store, "m1", "yes", "ty1")
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["entry_decision"] is None


def test_get_position_includes_entry_decision_signals(env) -> None:
    """A persisted verdict=ok EntryDecision linked by position_id surfaces
    its raw `signals` dict (edge/spread/min_edge/max_spread/...) and any
    reason — the same data NewsCard's '④ Entry' stage renders."""
    import json

    from openpoly.runtime.section_log import EntryDecision

    store, client, factory = env
    h = _open(store, "m1", "yes", "ty1")
    signals = {
        "side": "yes",
        "edge": 0.06,
        "spread": 0.01,
        "p_model": 0.7,
        "held_price": 0.64,
        "min_edge": 0.05,
        "max_spread": 0.05,
        "recent_move": 0.03,
    }
    decision = EntryDecision(
        ts=100.0,
        news_id="n1",
        ar_p_model=0.7,
        ar_market_id="m1",
        verdict="ok",
        side="yes",
        qty=3.0,
        price=0.64,
        reason=None,
        latency_ms=12,
        position_id=h.position_id,
        signals_json=json.dumps(signals),
    )
    with factory() as session:
        session.add(EntryDecisionRow(**decision.to_dict()))
        session.commit()
    body = client.get(f"/api/positions/{h.position_id}").json()
    assert body["entry_decision"] == {"signals": signals, "reason": None, "ts": 100.0}


def test_get_position_entry_decision_ignores_other_positions_and_skips(env) -> None:
    """Filters by this exact position_id and verdict=ok — a skip row (no
    position_id) or an ok row for a *different* position must not leak in."""
    import json

    from openpoly.runtime.section_log import EntryDecision

    store, client, factory = env
    h1 = _open(store, "m1", "yes", "ty1", ts=100.0)
    h2 = _open(store, "m2", "no", "tn2", ts=101.0)
    rows = [
        EntryDecisionRow(
            **EntryDecision(
                ts=90.0,
                news_id="n0",
                ar_p_model=0.6,
                ar_market_id="m1",
                verdict="skip",
                side="yes",
                qty=None,
                price=None,
                reason="edge below min_edge",
                latency_ms=5,
                position_id=None,
                signals_json=json.dumps({"side": "yes", "edge": 0.01}),
            ).to_dict()
        ),
        EntryDecisionRow(
            **EntryDecision(
                ts=101.0,
                news_id="n2",
                ar_p_model=0.3,
                ar_market_id="m2",
                verdict="ok",
                side="no",
                qty=10.0,
                price=0.5,
                reason=None,
                latency_ms=8,
                position_id=h2.position_id,
                signals_json=json.dumps({"side": "no", "edge": 0.1}),
            ).to_dict()
        ),
    ]
    with factory() as session:
        session.add_all(rows)
        session.commit()
    body = client.get(f"/api/positions/{h1.position_id}").json()
    assert body["entry_decision"] is None


# ---------- /api/positions/{id}/price-history ----------


def _market(**overrides):
    from openpoly.markets.models import Market

    base = dict(
        market_id="m1",
        condition_id="0xm1",
        question="Q?",
        slug="q",
        yes_token_id="ty1",
        no_token_id="tn1",
        end_date=None,
        best_bid=None,
        best_ask=None,
        spread=None,
        last_trade_price=None,
        volume_24h=0.0,
        liquidity=0.0,
        taker_fee_rate=None,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        event_id=None,
        event_title=None,
        event_tags=(),
    )
    base.update(overrides)
    return Market(**base)


def test_price_history_404_for_missing_position(env) -> None:
    _store, client, _factory = env
    assert client.get("/api/positions/99999/price-history").status_code == 404


def test_price_history_fetches_full_window_from_clob(env, monkeypatch) -> None:
    """The line is always CLOB-sourced end-to-end (opened_at through
    'now'/resolution) -- no local order-book snapshots involved anymore."""
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    opened_at = now - 10_000
    h = _open(store, "m1", "yes", "ty1", ts=opened_at)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    captured: dict = {}

    def fake_price_history_range(token_id, *, start_ts, end_ts, fidelity=10, **kwargs):
        captured["token_id"] = token_id
        captured["start_ts"] = start_ts
        captured["end_ts"] = end_ts
        captured["fidelity"] = fidelity
        return [(start_ts + 100, 0.55), (end_ts, 0.60)]

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", fake_price_history_range)

    body = client.get(f"/api/positions/{h.position_id}/price-history").json()
    assert "snapshots" not in body
    assert "price_points" not in body
    assert captured["token_id"] == "ty1"
    assert captured["start_ts"] == pytest.approx(opened_at)
    assert captured["end_ts"] == pytest.approx(now, abs=1.0)
    # 10_000s span / 250 target points -> ~0.67min, clamped up to the 1-min floor.
    assert captured["fidelity"] == 1
    assert body["price_history"] == [
        [captured["start_ts"] + 100, 0.55],
        [captured["end_ts"], 0.60],
    ]


@pytest.mark.parametrize(
    "window,expected_span,expected_fidelity",
    [
        ("1h", 3_600, 1),
        ("6h", 6 * 3_600, 2),
        ("1d", 86_400, 5),
        ("1w", 7 * 86_400, 30),
        ("1m", 30 * 86_400, 120),
    ],
)
def test_price_history_window_maps_to_expected_fidelity_and_span(
    env, monkeypatch, window, expected_span, expected_fidelity
) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    # Opened long enough ago that every named window's trailing span is fully
    # covered, not clipped by opened_at.
    opened_at = now - 40 * 86_400
    h = _open(store, "m1", "yes", "ty1", ts=opened_at)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    # A list, not a single dict: "1m"'s 30-day span exceeds CLOB's 15-day
    # per-request cap and gets served as two chunked calls -- capturing every
    # call lets the assertions below cover both the single-call and chunked
    # windows uniformly.
    calls: list[tuple[float, float, int]] = []

    def fake_price_history_range(token_id, *, start_ts, end_ts, fidelity=10, **kwargs):
        calls.append((start_ts, end_ts, fidelity))
        # Non-empty so the fidelity<10 fallback retry never fires here --
        # this test is about the window->fidelity/span mapping, not the
        # fallback path (covered separately).
        return [(start_ts, 0.5)]

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", fake_price_history_range)

    body = client.get(f"/api/positions/{h.position_id}/price-history?window={window}").json()
    assert body["window"] == window
    assert all(fidelity == expected_fidelity for _, _, fidelity in calls)
    total_span = calls[-1][1] - calls[0][0]
    assert total_span == pytest.approx(expected_span, abs=2.0)


def test_price_history_all_window_spans_full_lifetime_with_dynamic_fidelity(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    opened_at = now - 5 * 86_400
    h = _open(store, "m1", "yes", "ty1", ts=opened_at)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    captured: dict = {}

    def fake_price_history_range(token_id, *, start_ts, end_ts, fidelity=10, **kwargs):
        captured["start_ts"] = start_ts
        captured["fidelity"] = fidelity
        return []

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", fake_price_history_range)

    body = client.get(f"/api/positions/{h.position_id}/price-history?window=all").json()
    assert body["window"] == "all"
    assert captured["start_ts"] == pytest.approx(opened_at)
    assert captured["fidelity"] == round(5 * 86_400 / 60 / 250)


def test_price_history_unknown_window_falls_back_to_default(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 100)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", lambda *a, **k: [])

    body = client.get(f"/api/positions/{h.position_id}/price-history?window=garbage").json()
    assert body["window"] == "all"


def test_price_history_response_shape_is_unified(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 100)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", lambda *a, **k: [(now - 50, 0.5)])

    body = client.get(f"/api/positions/{h.position_id}/price-history").json()
    assert set(body.keys()) == {
        "position_id",
        "token_id",
        "window",
        "price_history",
        "market_end_date",
        "market_resolved",
        "winning_side",
    }
    assert body["price_history"] == [[now - 50, 0.5]]


def test_price_history_caches_clob_call_within_ttl(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 100)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    calls = {"n": 0}

    def counting_price_history_range(*args, **kwargs):
        calls["n"] += 1
        # Non-empty so the fidelity<10 fallback retry never fires here --
        # this test is about the outer TTL cache, not the fallback path.
        return [(now - 50, 0.5)]

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", counting_price_history_range)

    client.get(f"/api/positions/{h.position_id}/price-history?window=all")
    client.get(f"/api/positions/{h.position_id}/price-history?window=all")
    assert calls["n"] == 1


def test_price_history_cache_expires_after_ttl(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 100)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    calls = {"n": 0}

    def counting_price_history_range(*args, **kwargs):
        calls["n"] += 1
        return [(now - 50, 0.5)]

    class _FakeTime:
        def __init__(self, t: float) -> None:
            self.t = t

        def time(self) -> float:
            return self.t

    fake_time = _FakeTime(now)
    # Rebinds only the `time` name inside `routes`' own namespace -- not the
    # real global `time` module, which other code may rely on concurrently.
    monkeypatch.setattr(routes, "time", fake_time)
    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", counting_price_history_range)

    client.get(f"/api/positions/{h.position_id}/price-history?window=all")
    fake_time.t = now + routes.PRICE_HISTORY_CACHE_TTL_SECONDS["all"] + 1
    client.get(f"/api/positions/{h.position_id}/price-history?window=all")
    assert calls["n"] == 2


def test_price_history_clob_failure_returns_empty_without_500(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 100)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    def failing_price_history_range(*args, **kwargs):
        raise RuntimeError("CLOB unreachable")

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", failing_price_history_range)

    r = client.get(f"/api/positions/{h.position_id}/price-history")
    assert r.status_code == 200
    assert r.json()["price_history"] == []


def test_price_history_low_fidelity_empty_result_retries_at_fidelity_10(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    # "all" window over a short span -> fidelity clamps to the 1-min floor.
    h = _open(store, "m1", "yes", "ty1", ts=now - 100)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    def fake_price_history_range(token_id, *, start_ts, end_ts, fidelity=10, **kwargs):
        if fidelity < 10:
            return []
        return [(end_ts, 0.5)]

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", fake_price_history_range)

    body = client.get(f"/api/positions/{h.position_id}/price-history?window=all").json()
    assert len(body["price_history"]) == 1


def test_price_history_chunks_spans_exceeding_clob_max_interval(env, monkeypatch) -> None:
    """A position held longer than CLOB_MAX_INTERVAL_SECONDS (~15 days) needs
    multiple chunked CLOB requests, concatenated -- the live API hard-rejects
    a single request spanning more than that, regardless of fidelity
    (empirically verified against clob.polymarket.com)."""
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    opened_at = now - 20 * 86_400  # 20 days -> exceeds the 15-day cap
    h = _open(store, "m1", "yes", "ty1", ts=opened_at)

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=False)

    calls: list[tuple[float, float]] = []

    def fake_price_history_range(token_id, *, start_ts, end_ts, fidelity=10, **kwargs):
        assert end_ts - start_ts <= routes.CLOB_MAX_INTERVAL_SECONDS
        calls.append((start_ts, end_ts))
        return [(start_ts, 0.5)]

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", fake_price_history_range)

    body = client.get(f"/api/positions/{h.position_id}/price-history?window=all").json()
    assert len(calls) == 2  # 20 days split into two <=15-day chunks
    assert len(body["price_history"]) == 2
    assert calls[0][0] == pytest.approx(opened_at)
    assert calls[1][1] == pytest.approx(now, abs=2.0)
    assert calls[0][1] == calls[1][0]


def test_price_history_reports_resolution_and_winning_side(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 100)
    store.close_position(h.position_id, sell_price=0.30, ts=now - 50, close_reason="stop_loss")

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return _market(closed=True, accepting_orders=False, outcome_prices=(1.0, 0.0))

    async def fail_fetch_markets_by_condition_id(condition_ids, **kwargs):
        raise AssertionError("id lookup already reported closed -> no resolved-only fallback")

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_markets_by_condition_id", fail_fetch_markets_by_condition_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", lambda *a, **k: [])

    body = client.get(f"/api/positions/{h.position_id}/price-history").json()
    assert body["market_resolved"] is True
    assert body["winning_side"] == "yes"


def test_price_history_falls_back_to_resolved_only_lookup_past_end_date(env, monkeypatch) -> None:
    """The id-based lookup mirrors Gamma's open-only default and may miss a
    market that has actually resolved -> once end_date has passed, the
    closed=true fallback is tried too."""
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 200)
    store.close_position(h.position_id, sell_price=0.30, ts=now - 100, close_reason="stop_loss")

    async def fake_fetch_market_by_id(market_id, **kwargs):
        return None  # id lookup no longer sees the resolved market

    async def fake_fetch_markets_by_condition_id(condition_ids, **kwargs):
        assert condition_ids == ["0xm1"]
        return [{"__resolved__": True}]

    def fake_normalize(raw, **kwargs):
        assert raw == {"__resolved__": True}
        return _market(closed=True, outcome_prices=(0.0, 1.0))

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", fake_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_markets_by_condition_id", fake_fetch_markets_by_condition_id)
    monkeypatch.setattr(routes, "normalize_gamma_market", fake_normalize)
    monkeypatch.setattr(routes, "fetch_price_history_range", lambda *a, **k: [])

    body = client.get(f"/api/positions/{h.position_id}/price-history").json()
    assert body["market_resolved"] is True
    assert body["winning_side"] == "no"


def test_price_history_market_lookup_is_cached(env, monkeypatch) -> None:
    import time as _time

    import openpoly.api.portfolio_routes as routes

    store, client, _factory = env
    now = _time.time()
    h = _open(store, "m1", "yes", "ty1", ts=now - 10)

    calls = {"n": 0}

    async def counting_fetch_market_by_id(market_id, **kwargs):
        calls["n"] += 1
        return _market(closed=False)

    monkeypatch.setattr(routes, "_market_lookup_cache", {})
    monkeypatch.setattr(routes, "_price_history_cache", {})
    monkeypatch.setattr(routes, "fetch_market_by_id", counting_fetch_market_by_id)
    monkeypatch.setattr(routes, "fetch_price_history_range", lambda *a, **k: [])

    client.get(f"/api/positions/{h.position_id}/price-history")
    client.get(f"/api/positions/{h.position_id}/price-history")
    assert calls["n"] == 1


# ---------- news confluence ----------


def test_position_detail_surfaces_the_confluence_ledger(env) -> None:
    store, client, factory = env
    held = _open(store, "m1", "yes", "ty1", ts=100.0)
    now = time.time()
    store.record_signal(
        held.position_id,
        news_id="n1",
        ts=now - 600,
        side="yes",
        relation="opening",
        p_model=0.71,
        confidence="high",
    )
    store.record_signal(
        held.position_id,
        news_id="n2",
        ts=now - 300,
        side="no",
        relation="contradict",
        p_model=0.2,
        confidence="medium",
    )
    with factory() as session:
        session.add(
            NewsItemRow(
                news_id="n1",
                content="the original headline",
                urgency="high",
                sentiment=None,
                published_at=now - 610,
                received_at=now - 605,
            )
        )
        session.commit()

    body = client.get(f"/api/positions/{held.position_id}").json()

    assert body["confluence"] == {"state": "contested", "support": 1, "against": 1}
    signals = body["news_signals"]
    assert [s["relation"] for s in signals] == ["opening", "contradict"]
    assert signals[0]["content"] == "the original headline"
    assert (signals[0]["p_model"], signals[0]["confidence"]) == (0.71, "high")
    # A signal whose news row was never persisted still appears, sans content.
    assert signals[1]["news_id"] == "n2" and signals[1]["content"] is None
    assert signals[1]["side"] == "no"  # the side the decision WANTED


def test_news_signals_carry_sentiment_and_rationale(env) -> None:
    """Each news-confluence signal is enriched with its news item's sentiment
    and, when a matching verdict=ok analyzer call exists for the *same*
    market_id, that call's rationale/self_check. A same-news_id call for a
    different market must not leak in, and a signal with no matching
    analyzer call degrades to None rather than erroring."""
    from openpoly.runtime.section_log import AnalyzerCall

    store, client, factory = env
    held = _open(store, "m1", "yes", "ty1", ts=100.0)
    now = time.time()
    store.record_signal(
        held.position_id,
        news_id="n1",
        ts=now - 600,
        side="yes",
        relation="opening",
        p_model=0.71,
        confidence="high",
    )
    store.record_signal(
        held.position_id,
        news_id="n2",
        ts=now - 300,
        side="no",
        relation="contradict",
        p_model=0.2,
        confidence="medium",
    )
    with factory() as session:
        session.add_all(
            [
                NewsItemRow(
                    news_id="n1",
                    content="the original headline",
                    urgency="high",
                    sentiment="positive",
                    published_at=now - 610,
                    received_at=now - 605,
                ),
                NewsItemRow(
                    news_id="n2",
                    content="a rebuttal",
                    urgency="medium",
                    sentiment="negative",
                    published_at=now - 310,
                    received_at=now - 305,
                ),
            ]
        )
        session.add_all(
            [
                AnalyzerCallRow(
                    **AnalyzerCall(
                        ts=now - 600,
                        news_id="n1",
                        news_content_preview="x",
                        urgency="high",
                        verdict="ok",
                        p_model=0.71,
                        confidence="high",
                        market_id="m1",
                        latency_ms=20,
                        rationale="opened on the original headline",
                        self_check="checked against the order book",
                    ).to_dict()
                ),
                # Same news_id, different market — must NOT be picked up.
                AnalyzerCallRow(
                    **AnalyzerCall(
                        ts=now - 599,
                        news_id="n1",
                        news_content_preview="x",
                        urgency="high",
                        verdict="ok",
                        p_model=0.71,
                        confidence="high",
                        market_id="m9",
                        latency_ms=20,
                        rationale="wrong market — must not appear",
                    ).to_dict()
                ),
            ]
        )
        session.commit()

    body = client.get(f"/api/positions/{held.position_id}").json()
    signals = body["news_signals"]
    assert signals[0]["news_id"] == "n1"
    assert signals[0]["sentiment"] == "positive"
    assert signals[0]["rationale"] == "opened on the original headline"
    assert signals[0]["self_check"] == "checked against the order book"
    # n2 has a news row (sentiment) but no matching analyzer call.
    assert signals[1]["news_id"] == "n2"
    assert signals[1]["sentiment"] == "negative"
    assert signals[1]["rationale"] is None
    assert signals[1]["self_check"] is None


def test_position_with_no_signals_reads_as_solo(env) -> None:
    store, client, _factory = env
    held = _open(store, "m1", "yes", "ty1", ts=100.0)
    body = client.get(f"/api/positions/{held.position_id}").json()
    assert body["confluence"] == {"state": "solo", "support": 0, "against": 0}
    assert body["news_signals"] == []


def test_positions_list_carries_the_confluence_state(env) -> None:
    store, client, _factory = env
    held = _open(store, "m1", "yes", "ty1", ts=100.0)
    _open(store, "m2", "yes", "ty2", ts=101.0)
    now = time.time()
    for i, news_id in enumerate(("n1", "n2")):
        store.record_signal(
            held.position_id,
            news_id=news_id,
            ts=now - 60 * (2 - i),
            side="yes",
            relation="opening" if i == 0 else "reinforce",
            confidence="high",
        )

    rows = {r["id"]: r for r in client.get("/api/positions").json()["positions"]}
    assert rows[held.position_id]["confluence"]["state"] == "reinforced"
    assert rows[held.position_id]["news_signal_count"] == 2
    other = next(r for pid, r in rows.items() if pid != held.position_id)
    assert other["confluence"]["state"] == "solo"
    assert other["news_signal_count"] == 0
