"""Endpoint tests for GET /api/statistics (Statistics page)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from openpoly.api.main import app
from openpoly.db.engine import get_session_factory, init_db, make_engine, make_session_factory
from openpoly.portfolio import PortfolioStore


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/statistics.db")
    init_db(engine)
    factory = make_session_factory(engine)
    store = PortfolioStore(factory)
    app.dependency_overrides[get_session_factory] = lambda: factory
    yield store, TestClient(app), factory
    app.dependency_overrides.clear()
    engine.dispose()


def _open(store, market_id, ts, price=0.40, qty=10.0):
    return store.open_position(
        market_id=market_id,
        side="yes",
        token_id=f"t{market_id}",
        condition_id=f"0x{market_id}",
        price=price,
        qty=qty,
        ts=ts,
    )


def test_statistics_empty(env) -> None:
    _store, client, _factory = env
    body = client.get("/api/statistics").json()
    assert body["since"] is None
    assert body["until"] is None
    assert body["summary"]["positions_opened"] == 0
    assert body["summary"]["win_rate"] is None
    assert body["pnl_curve"] == []
    assert body["closed_positions"] == []
    assert body["closed_positions_truncated"] is False


def test_statistics_all_time_when_omitted(env) -> None:
    store, client, _factory = env
    h = _open(store, "m1", ts=100.0)
    store.close_position(h.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
    body = client.get("/api/statistics").json()
    assert body["summary"]["positions_closed"] == 1
    assert body["summary"]["wins"] == 1


def test_statistics_since_until_applied(env) -> None:
    store, client, _factory = env
    h1 = _open(store, "m1", ts=50.0)
    store.close_position(h1.position_id, sell_price=0.55, ts=150.0, close_reason="take_profit")
    h2 = _open(store, "m2", ts=250.0)
    store.close_position(h2.position_id, sell_price=0.55, ts=350.0, close_reason="take_profit")

    body = client.get("/api/statistics?since=100&until=200").json()
    assert body["since"] == 100.0
    assert body["until"] == 200.0
    assert body["summary"]["positions_closed"] == 1
    assert len(body["closed_positions"]) == 1
    assert body["closed_positions"][0]["market_id"] == "m1"


def test_statistics_only_losses_profit_factor_is_zero_not_none(env) -> None:
    store, client, _factory = env
    h = _open(store, "m1", ts=100.0)
    store.close_position(h.position_id, sell_price=0.25, ts=200.0, close_reason="stop_loss")
    body = client.get("/api/statistics").json()
    assert body["summary"]["profit_factor"] == 0.0


def test_statistics_only_wins_profit_factor_is_null_not_infinity(env) -> None:
    """gross_loss == 0 must produce a JSON `null`, not the invalid literal
    `Infinity` token Python's default encoder emits for float('inf') — the
    build_statistics None-guard is what prevents this; ``.json()`` below
    would raise a JSONDecodeError if that guard ever regressed."""
    store, client, _factory = env
    h = _open(store, "m1", ts=100.0)
    store.close_position(h.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
    body = client.get("/api/statistics").json()
    assert body["summary"]["profit_factor"] is None


def test_statistics_closed_positions_include_market_question(env) -> None:
    """Mirrors test_get_position_includes_market_question_when_catalogued in
    test_api_portfolio.py."""
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import MarketStore, PollSummary

    store, client, _factory = env
    h = _open(store, "m1", ts=100.0)  # _open sets condition_id="0xm1"
    store.close_position(h.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
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
        body = client.get("/api/statistics").json()
    finally:
        msm.store = saved_store
    assert body["closed_positions"][0]["market_question"] == "Will the U.S. invade Iran before 2027?"


def test_statistics_closed_positions_market_question_null_when_not_catalogued(env) -> None:
    from openpoly.markets.manager import manager as msm
    from openpoly.markets.store import MarketStore

    store, client, _factory = env
    h = _open(store, "m1", ts=100.0)
    store.close_position(h.position_id, sell_price=0.55, ts=200.0, close_reason="take_profit")
    saved_store = msm.store
    try:
        msm.store = MarketStore()  # empty catalog
        body = client.get("/api/statistics").json()
    finally:
        msm.store = saved_store
    assert body["closed_positions"][0]["market_question"] is None
