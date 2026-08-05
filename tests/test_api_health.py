"""Endpoint tests for GET /api/health (frozen liveness probe) and
GET /api/health/detail (composite subsystem report)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openpoly.api import health_routes as hr
from openpoly.api import wallet_routes as wr
from openpoly.api.main import app


class FakeDB:
    def __init__(self, writers: dict[str, Any] | None = None, tables: dict[str, int] | None = None):
        self._writers = (
            writers
            if writers is not None
            else {"order_book": {"written": 10, "dropped": 0, "pending": 0, "sink_errors": 0}}
        )
        self._tables = tables if tables is not None else {"position": 3}

    def status(self) -> dict[str, Any]:
        return {"tables": self._tables, "writers": self._writers}


class RaisingDB:
    def status(self) -> dict[str, Any]:
        raise RuntimeError("db unreachable")


class FakeMarketSourceManager:
    def __init__(self, **kwargs: Any):
        defaults = dict(
            state="running",
            last_poll_at=time.time(),
            catalog_size=10,
            poll_count=5,
            last_error=None,
            running_config={"poll_interval_seconds": 900},
        )
        defaults.update(kwargs)
        self._snap = SimpleNamespace(**defaults)

    def status(self) -> SimpleNamespace:
        return self._snap


class FakeNewsSourceManager:
    def __init__(self, **kwargs: Any):
        defaults = dict(
            state="connected",
            last_msg_at=time.time(),
            total_recv=5,
            buffer_size=5,
            reconnect_attempts=0,
            last_error=None,
        )
        defaults.update(kwargs)
        self._snap = SimpleNamespace(**defaults)

    def status(self) -> SimpleNamespace:
        return self._snap


class FakeOrchestrator:
    def __init__(self, state: str = "running", queue_depth: int = 0):
        self.state = state
        self.queue_depth = queue_depth


class FakeExitMonitor:
    def __init__(
        self,
        state: str = "running",
        last_tick_at: float | None = None,
        open_positions: int = 0,
        blocked: int = 0,
        last_tick: dict[str, Any] | None = None,
    ):
        self.state = state
        self.last_tick_at = last_tick_at if last_tick_at is not None else time.time()
        self.open_positions = open_positions
        self.blocked = blocked
        self.last_tick = last_tick


class FakeSettlementMonitor:
    def __init__(self, state: str = "running"):
        self.state = state


class FakeEmbeddingManager:
    def __init__(self, state: str = "running"):
        self._status = {
            "state": state,
            "model_name": "all-MiniLM-L6-v2",
            "warm_count": 3,
            "encoder_loaded": True,
        }

    def status(self) -> dict[str, Any]:
        return self._status


class FakeExecutor:
    def __init__(self, balance_raw: int | None = 1_000_000, live_ready: bool = True):
        self._balance_raw = balance_raw
        self.live_ready = live_ready

    def get_collateral_balance_raw(self) -> int | None:
        return self._balance_raw


class FakeRuntimeState:
    def __init__(self, wallet: Any = "configured", exec_mode: str = "paper"):
        self.wallet = wallet
        self.exec_mode = exec_mode


class FakeReconciliationMonitor:
    def __init__(self, state: str = "running"):
        self.state = state


@pytest.fixture
def health_env():
    """TestClient with every health subsystem dependency defaulted to a
    healthy fake. Tests mutate the returned ``overrides`` dict in place to
    change just the subsystem(s) they care about before requesting.
    """
    overrides: dict[Any, Any] = {
        hr.get_database_manager: lambda: FakeDB(),
        hr.get_market_source_manager: lambda: FakeMarketSourceManager(),
        hr.get_news_source_manager: lambda: FakeNewsSourceManager(),
        hr.get_pipeline_orchestrator: lambda: FakeOrchestrator(),
        hr.get_exit_monitor: lambda: FakeExitMonitor(),
        hr.get_settlement_monitor: lambda: FakeSettlementMonitor(),
        hr.get_embedding_manager: lambda: FakeEmbeddingManager(),
        hr.get_executor_dispatcher: lambda: FakeExecutor(),
        hr.get_runtime_state: lambda: FakeRuntimeState(),
        hr.get_reconciliation_monitor: lambda: FakeReconciliationMonitor(),
    }
    app.dependency_overrides.update(overrides)
    # The wallet-balance TTL cache is module-global state shared across
    # requests — reset it so a previous test's fake executor reading can't
    # leak into this one through the cache.
    wr._raw_balance_cache = None
    # Yield the actual dependency_overrides dict (not the local `overrides`)
    # so a test mutating it in place — e.g. `overrides[hr.get_x] = ...` —
    # changes what FastAPI reads; `.update()` above copied pairs in, it did
    # not alias the dict.
    yield app.dependency_overrides, TestClient(app)
    app.dependency_overrides.clear()
    wr._raw_balance_cache = None


def test_health_frozen_endpoint_unchanged(health_env) -> None:
    """Regression guard: scripts/deploy.sh greps this literal response."""
    _overrides, client = health_env
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_detail_all_healthy(health_env) -> None:
    _overrides, client = health_env
    r = client.get("/api/health/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    for name, check in body["checks"].items():
        assert check["status"] in ("ok", "disabled"), f"{name}: {check}"


def test_health_detail_news_feed_error_marks_down(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_news_source_manager] = lambda: FakeNewsSourceManager(
        state="error", last_error="auth_fail"
    )
    body = client.get("/api/health/detail").json()
    assert body["checks"]["news_feed"]["status"] == "down"
    assert body["status"] == "down"


def test_health_detail_database_writer_drops_degraded(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_database_manager] = lambda: FakeDB(
        writers={"order_book": {"written": 10, "dropped": 5, "pending": 0, "sink_errors": 0}}
    )
    body = client.get("/api/health/detail").json()
    assert body["checks"]["database"]["status"] == "degraded"
    assert body["checks"]["database"]["detail"]["unhealthy_writers"] == ["order_book"]
    assert body["status"] == "degraded"


def test_health_detail_pipeline_backpressure_degraded(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_pipeline_orchestrator] = lambda: FakeOrchestrator(queue_depth=95)
    body = client.get("/api/health/detail").json()
    assert body["checks"]["pipeline"]["status"] == "degraded"
    assert body["status"] == "degraded"


def test_health_detail_exit_monitor_blocked_degraded(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_exit_monitor] = lambda: FakeExitMonitor(blocked=2)
    body = client.get("/api/health/detail").json()
    assert body["checks"]["exit_monitor"]["status"] == "degraded"
    assert body["status"] == "degraded"


def test_health_detail_market_access_no_wallet_disabled(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_runtime_state] = lambda: FakeRuntimeState(wallet=None)
    body = client.get("/api/health/detail").json()
    assert body["checks"]["market_access"]["status"] == "disabled"
    assert body["checks"]["market_access"]["detail"]["configured"] is False
    # "disabled" must not drag the overall rollup down.
    assert body["status"] == "ok"


def test_health_detail_live_mode_without_armed_executor_is_down(health_env) -> None:
    """exec_mode=live with no live executor is the quietest way this system can
    fail: the UI shows LIVE, the pipeline runs, and every order skips as
    live_not_ready. It must read as down, not ok."""
    overrides, client = health_env
    overrides[hr.get_runtime_state] = lambda: FakeRuntimeState(exec_mode="live")
    overrides[hr.get_executor_dispatcher] = lambda: FakeExecutor(live_ready=False)
    body = client.get("/api/health/detail").json()
    assert body["checks"]["market_access"]["status"] == "down"
    assert body["checks"]["market_access"]["detail"]["live_ready"] is False
    assert body["status"] == "down"


def test_health_detail_live_mode_with_armed_executor_is_ok(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_runtime_state] = lambda: FakeRuntimeState(exec_mode="live")
    body = client.get("/api/health/detail").json()
    assert body["checks"]["market_access"]["status"] == "ok"
    assert body["checks"]["market_access"]["detail"]["live_ready"] is True
    assert body["status"] == "ok"


def test_health_detail_paper_mode_unarmed_is_not_down(health_env) -> None:
    """Paper mode never dispatches to live, so an unarmed executor there is
    normal — only the balance read degrades."""
    overrides, client = health_env
    overrides[hr.get_executor_dispatcher] = lambda: FakeExecutor(balance_raw=None, live_ready=False)
    body = client.get("/api/health/detail").json()
    assert body["checks"]["market_access"]["status"] == "degraded"


def test_health_detail_reconciliation_absent_disabled(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_reconciliation_monitor] = lambda: None
    body = client.get("/api/health/detail").json()
    assert body["checks"]["reconciliation"]["status"] == "disabled"
    assert body["status"] == "ok"


def test_health_detail_db_exception_becomes_down_not_500(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_database_manager] = lambda: RaisingDB()
    r = client.get("/api/health/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["database"]["status"] == "down"
    assert "error" in body["checks"]["database"]["detail"]
    assert body["status"] == "down"


def test_health_detail_market_feed_stale_degraded(health_env) -> None:
    overrides, client = health_env
    overrides[hr.get_market_source_manager] = lambda: FakeMarketSourceManager(
        last_poll_at=time.time() - 3600, running_config={"poll_interval_seconds": 900}
    )
    body = client.get("/api/health/detail").json()
    assert body["checks"]["market_feed"]["status"] == "degraded"
    assert body["status"] == "degraded"
