"""Endpoint tests for POST /api/debug/inject_news.

This route is for local testing without a TradingNews subscription — it
pushes a synthetic NewsItem through the real pipeline, which reaches
executor.execute_buy and dispatches to the live broker whenever
exec_mode is "live". Must be blocked in that mode; paper mode (its
actual intended use) must be unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import openpoly.api.debug_routes as debug_routes
from openpoly.api.main import app
from openpoly.wallet.runtime_state import RuntimeState


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, RuntimeState]:
    monkeypatch.setenv("OPENPOLY_RUNTIME_STATE", str(tmp_path / "runtime.json"))
    rs = RuntimeState()
    rs.load()
    monkeypatch.setattr(debug_routes, "runtime_state", rs)
    return TestClient(app), rs


def test_inject_news_allowed_in_paper_mode(env: tuple[TestClient, RuntimeState]) -> None:
    client, rs = env
    assert rs.exec_mode == "paper"  # default
    r = client.post("/api/debug/inject_news", json={"content": "hello"})
    assert r.status_code == 200
    assert r.json()["status"] in ("enqueued", "rejected")  # rejected only if queue is full


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def enqueue(self, item: object) -> bool:
        self.calls.append(item)
        return True


def test_inject_news_blocked_in_live_mode(
    env: tuple[TestClient, RuntimeState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, rs = env
    rs._exec_mode = "live"  # noqa: SLF001 — bypass set_mode's disk write + preflight checks

    fake_orch = _FakeOrchestrator()
    monkeypatch.setattr(debug_routes, "get_orchestrator", lambda: fake_orch)

    r = client.post("/api/debug/inject_news", json={"content": "hello"})

    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "live_mode_active"
    assert fake_orch.calls == []  # the pipeline must never be reached
