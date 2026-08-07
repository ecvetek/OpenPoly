"""Endpoint tests for POST /api/backtest/run.

The route delegates the actual replay to openpoly.backtest.engine (covered
in depth by test_backtest_engine.py) — these tests focus on the HTTP-layer
concerns: the concurrent-backtest guard, range validation, and the response
envelope shape.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from openpoly.api.main import app
from openpoly.backtest.guard import set_backtest_active
from openpoly.db.engine import get_session_factory, init_db, make_engine, make_session_factory
from openpoly.db.market_catalog_store import upsert_market_catalog_row
from openpoly.db.tables import AnalyzerCallRow, OrderBookSnapshot
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary


def _market(market_id: str = "m1"):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Will X happen?",
        "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
    }
    m = normalize_gamma_market(raw, event={"id": "e1", "title": "E", "tags": []})
    assert m is not None
    return m


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/backtest_api.db")
    init_db(engine)
    factory = make_session_factory(engine)
    app.dependency_overrides[get_session_factory] = lambda: factory

    saved_store = market_source_manager.store
    market_source_manager.store = MarketStore()

    yield factory
    app.dependency_overrides.clear()
    market_source_manager.store = saved_store
    set_backtest_active(False)  # in case a test left it set
    engine.dispose()


def _seed(factory) -> None:
    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    with factory() as session:
        session.add(
            AnalyzerCallRow(
                ts=100.0,
                news_id="n1",
                news_content_preview="something happened",
                urgency="high",
                verdict="ok",
                p_model=0.7,
                confidence="high",
                market_id="m1",
                latency_ms=50,
                error=None,
                rationale="looks bullish",
            )
        )
        session.add(
            OrderBookSnapshot(
                token_id="yes-m1",
                recorded_at=100.0,
                bids_json=json.dumps([[0.40, 100.0]]),
                asks_json=json.dumps([[0.42, 100.0]]),
            )
        )
        session.add(
            OrderBookSnapshot(
                token_id="yes-m1",
                recorded_at=150.0,
                bids_json=json.dumps([[0.55, 100.0]]),
                asks_json=json.dumps([[0.57, 100.0]]),
            )
        )
        session.commit()


def _body(**overrides) -> dict:
    defaults = {
        "since": 0.0,
        "until": 200.0,
        "entry": {
            "module": "openpoly.sections.entry.edge_threshold_v0",
            "name": "EdgeThresholdEntryV0",
            "config": {},
        },
        "exit": {
            "module": "openpoly.sections.exit.threshold_v0",
            "name": "ThresholdExitV0",
            "config": {},
        },
    }
    defaults.update(overrides)
    return defaults


def test_run_happy_path_shape(env) -> None:
    _seed(env)
    client = TestClient(app)
    r = client.post("/api/backtest/run", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["replayed_analyzer_calls"] == 1
    assert body["skipped_market_not_in_catalog"] == 0
    assert body["summary"]["positions_closed"] == 1
    assert body["summary"]["wins"] == 1
    assert body["closed_positions"][0]["close_reason"] == "take_profit"
    assert isinstance(body["elapsed_ms"], int)


def test_run_resolves_market_and_question_via_persisted_catalog_when_not_live(env) -> None:
    """The reported symptom: a backtest whose analyzer call references a
    market that has since left live discovery (resolved, expired, filtered
    out). market_source_manager.store deliberately has NOTHING registered —
    only the persisted market_catalog table knows this market — proving
    both the replay itself and the results table's market_question resolve
    via that fallback, not just the live catalog."""
    with env() as session:
        upsert_market_catalog_row(session, _market("m1"), now=50.0)
        session.commit()
    with env() as session:
        session.add(
            AnalyzerCallRow(
                ts=100.0,
                news_id="n1",
                news_content_preview="something happened",
                urgency="high",
                verdict="ok",
                p_model=0.7,
                confidence="high",
                market_id="m1",
                latency_ms=50,
                error=None,
                rationale="looks bullish",
            )
        )
        session.add(
            OrderBookSnapshot(
                token_id="yes-m1",
                recorded_at=100.0,
                bids_json=json.dumps([[0.40, 100.0]]),
                asks_json=json.dumps([[0.42, 100.0]]),
            )
        )
        session.add(
            OrderBookSnapshot(
                token_id="yes-m1",
                recorded_at=150.0,
                bids_json=json.dumps([[0.55, 100.0]]),
                asks_json=json.dumps([[0.57, 100.0]]),
            )
        )
        session.commit()

    client = TestClient(app)
    r = client.post("/api/backtest/run", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["skipped_market_not_in_catalog"] == 0
    assert body["replayed_analyzer_calls"] == 1
    assert body["summary"]["positions_closed"] == 1
    assert body["closed_positions"][0]["market_question"] == "Will X happen?"


def test_run_returns_409_when_a_backtest_is_already_active(env) -> None:
    set_backtest_active(True)
    client = TestClient(app)
    r = client.post("/api/backtest/run", json=_body())
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "backtest_in_progress"


def test_run_returns_400_when_until_before_since(env) -> None:
    client = TestClient(app)
    r = client.post("/api/backtest/run", json=_body(since=200.0, until=100.0))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_range"


def test_run_returns_400_when_range_exceeds_cap(env) -> None:
    client = TestClient(app)
    ninety_one_days = 91 * 86400
    r = client.post("/api/backtest/run", json=_body(since=0.0, until=float(ninety_one_days)))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "range_too_large"


def test_run_returns_400_on_unknown_impl(env) -> None:
    client = TestClient(app)
    r = client.post(
        "/api/backtest/run",
        json=_body(
            entry={
                "module": "openpoly.sections.entry.edge_threshold_v0",
                "name": "DoesNotExist",
                "config": {},
            }
        ),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "backtest_failed"


def test_run_restores_market_store_after_request(env) -> None:
    _seed(env)
    original = market_source_manager.store
    client = TestClient(app)
    client.post("/api/backtest/run", json=_body())
    assert market_source_manager.store is original
