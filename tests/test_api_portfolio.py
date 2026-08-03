"""Endpoint tests for /api/positions and /api/fills (PF5)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openpoly.api.main import app
from openpoly.api.portfolio_routes import get_portfolio_store
from openpoly.db.engine import get_session_factory, init_db, make_engine, make_session_factory
from openpoly.db.tables import AnalyzerCallRow, ExitDecisionRow, NewsItemRow
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

    from openpoly.api.portfolio_routes import EQUITY_WINDOW_SECONDS

    store, client, _factory = env
    old_ts = time.time() - EQUITY_WINDOW_SECONDS * 3
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
    store.close_position(
        h.position_id, sell_price=0.55, ts=old_ts + 10, close_reason="take_profit"
    )
    body = client.get("/api/portfolio/equity").json()
    assert body["summary"]["realized"] == pytest.approx(1.50)
    assert body["points"] == []


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
    assert set(decisions[0].keys()) == {"rationale", "p_model", "confidence", "ts"}


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
    assert set(decisions[0].keys()) == {"rationale", "p_model", "confidence", "ts"}

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
        h.position_id, sell_price=0.55, ts=200.0,
        close_reason="take_profit", trigger="take_profit",
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
        market_id="m1", side="yes", token_id="ty1", condition_id="0xm1",
        price=0.42, qty=10.0, ts=100.0, news_id=None,
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
        h.position_id, sell_price=0.30, ts=200.0,
        close_reason="stop_loss", trigger="stop_loss",
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
                ts=150.0, position_id=h1.position_id, market_id="m1", side="yes",
                verdict="skip", trigger=None, return_pct=0.01, fill_price=None,
                realized_pnl=None, reason="within threshold",
            ).to_dict()
        ),
        ExitDecisionRow(
            **ExitDecision(
                ts=200.0, position_id=h2.position_id, market_id="m2", side="no",
                verdict="ok", trigger="take_profit", return_pct=0.2, fill_price=0.6,
                realized_pnl=3.0, reason="tp hit",
            ).to_dict()
        ),
    ]
    with factory() as session:
        session.add_all(rows)
        session.commit()
    body = client.get(f"/api/positions/{h1.position_id}").json()
    assert body["exit_decision"] is None
