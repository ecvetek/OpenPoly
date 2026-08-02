"""Lifespan auto-start of the news + market sources.

``OPENPOLY_AUTOSTART_SOURCES`` defaults on; conftest sets it to ``0`` for the
session, so each test here sets it explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openpoly.api.main import _autostart_enabled, _autostart_sources, app, lifespan
from openpoly.markets.manager import manager as market_manager
from openpoly.news.manager import manager as news_manager


@pytest.fixture(autouse=True)
def _isolated_canvas_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """market_source autostart now reads the persisted canvas config — every
    test in this file must not touch the real ``~/.openpoly/canvas.json``."""
    monkeypatch.setenv("OPENPOLY_CANVAS_STORE", str(tmp_path / "canvas.json"))


def test_autostart_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENPOLY_AUTOSTART_SOURCES", raising=False)
    assert _autostart_enabled() is True  # on by default
    monkeypatch.setenv("OPENPOLY_AUTOSTART_SOURCES", "0")
    assert _autostart_enabled() is False
    monkeypatch.setenv("OPENPOLY_AUTOSTART_SOURCES", "1")
    assert _autostart_enabled() is True


async def test_lifespan_autostarts_both_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENPOLY_AUTOSTART_SOURCES", "1")
    market_calls: list[Any] = []
    news_calls: list[Any] = []

    async def fake_market_start(config: Any) -> None:
        market_calls.append(config)

    async def fake_news_start(config: Any) -> None:
        news_calls.append(config)

    monkeypatch.setattr(market_manager, "start", fake_market_start)
    monkeypatch.setattr(news_manager, "start", fake_news_start)

    async with lifespan(app):
        pass

    assert len(market_calls) == 1
    assert len(news_calls) == 1
    # No canvas saved and no override env var → TradingNewsWSConfig()'s own
    # default, not a hardcoded guess.
    assert news_calls[0]["api_key_ref"] == "env:OPENPOLY_TRADINGNEWS_KEY"
    assert "endpoint" in news_calls[0]


async def test_lifespan_skips_autostart_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENPOLY_AUTOSTART_SOURCES", "0")
    market_calls: list[Any] = []
    news_calls: list[Any] = []

    async def fake_market_start(config: Any) -> None:
        market_calls.append(config)

    async def fake_news_start(config: Any) -> None:
        news_calls.append(config)

    monkeypatch.setattr(market_manager, "start", fake_market_start)
    monkeypatch.setattr(news_manager, "start", fake_news_start)

    async with lifespan(app):
        pass

    assert market_calls == []
    assert news_calls == []


# ---------- market_source autostart uses the persisted canvas config ----------


def _template_with_market_source(**config_overrides: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "test",
        "nodes": [
            {
                "id": "market-source-seed",
                "sectionType": "market_source",
                "position": {"x": 0, "y": 0},
                "config": config_overrides,
            }
        ],
        "edges": [],
    }


async def test_market_autostart_uses_canvas_saved_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a fresh backend boot must seed market_source from the
    operator's saved canvas config, not bare MarketSourceConfig() defaults —
    otherwise the manager's start()-is-a-no-op-once-running guard means no
    later Resume/Start can ever apply the real config short of an explicit
    Stop first (the actual bug reported: catalog stuck small after a
    post-restart Resume, only correcting itself after a manual Stop+Start)."""
    from openpoly.runtime.canvas_store import save_template

    save_template(_template_with_market_source(poll_interval_seconds=120))

    captured: list[Any] = []

    async def fake_market_start(config: Any) -> None:
        captured.append(config)

    monkeypatch.setattr(market_manager, "start", fake_market_start)
    await _autostart_sources()

    assert len(captured) == 1
    assert captured[0].poll_interval_seconds == 120  # from canvas, not the 900 default


async def test_lifespan_calls_bootstrap_peaks_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exit_monitor.bootstrap_peaks() must run before exit_monitor.start() —
    it reconstructs each open position's true historical peak price, which
    the tick loop needs seeded before it starts evaluating stop-loss."""
    from openpoly.runtime.exit_monitor import exit_monitor

    order: list[str] = []

    def fake_bootstrap_peaks(session_factory: Any) -> None:
        order.append("bootstrap_peaks")

    async def fake_start() -> None:
        order.append("start")

    async def fake_stop() -> None:
        pass

    monkeypatch.setattr(exit_monitor, "bootstrap_peaks", fake_bootstrap_peaks)
    monkeypatch.setattr(exit_monitor, "start", fake_start)
    monkeypatch.setattr(exit_monitor, "stop", fake_stop)

    async with lifespan(app):
        pass

    assert order == ["bootstrap_peaks", "start"]


async def test_lifespan_shutdown_stops_monitors_before_database_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every consumer/producer loop must be fully stopped before
    database_manager.stop() tears down the write-behind writers —
    otherwise anything still in flight during that window would silently
    lose its persist call (the original bug: this used to run in the
    opposite order, contradicting the shutdown block's own comment)."""
    from openpoly.db.manager import manager as database_manager
    from openpoly.embedding.manager import manager as embedding_manager_singleton
    from openpoly.markets.manager import manager as market_manager_singleton
    from openpoly.news.manager import manager as news_manager_singleton
    from openpoly.runtime.exit_monitor import exit_monitor
    from openpoly.runtime.orchestrator import PipelineOrchestrator
    from openpoly.runtime.settlement_monitor import settlement_monitor

    order: list[str] = []

    async def fake_settlement_stop() -> None:
        order.append("settlement_monitor.stop")

    async def fake_exit_stop() -> None:
        order.append("exit_monitor.stop")

    async def fake_embedding_stop() -> None:
        order.append("embedding_manager.stop")

    async def fake_orch_stop(self: PipelineOrchestrator) -> None:
        order.append("orchestrator.stop")

    async def fake_news_shutdown() -> None:
        order.append("news_source_manager.shutdown")

    async def fake_market_shutdown() -> None:
        order.append("market_source_manager.shutdown")

    async def fake_db_stop() -> None:
        order.append("database_manager.stop")

    monkeypatch.setattr(exit_monitor, "bootstrap_peaks", lambda session_factory: None)
    monkeypatch.setattr(settlement_monitor, "stop", fake_settlement_stop)
    monkeypatch.setattr(exit_monitor, "stop", fake_exit_stop)
    monkeypatch.setattr(embedding_manager_singleton, "stop", fake_embedding_stop)
    monkeypatch.setattr(PipelineOrchestrator, "stop", fake_orch_stop)
    monkeypatch.setattr(news_manager_singleton, "shutdown", fake_news_shutdown)
    monkeypatch.setattr(market_manager_singleton, "shutdown", fake_market_shutdown)
    monkeypatch.setattr(database_manager, "stop", fake_db_stop)

    async with lifespan(app):
        pass

    assert order[-1] == "database_manager.stop"
    assert set(order[:-1]) == {
        "settlement_monitor.stop",
        "exit_monitor.stop",
        "embedding_manager.stop",
        "orchestrator.stop",
        "news_source_manager.shutdown",
        "market_source_manager.shutdown",
    }


async def test_market_autostart_falls_back_to_defaults_when_no_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No persisted canvas (fresh install) → still starts, using
    MarketSourceConfig()'s own defaults rather than failing."""
    captured: list[Any] = []

    async def fake_market_start(config: Any) -> None:
        captured.append(config)

    monkeypatch.setattr(market_manager, "start", fake_market_start)
    await _autostart_sources()

    assert len(captured) == 1
    assert captured[0].poll_interval_seconds == 900


# ---------- news_source autostart uses the persisted canvas config ----------
# Same underlying bug/fix as market_source above. api_key_ref is now
# ALSO canvas-sourced (matching what a manual Start/Resume already does) —
# the previous hardcoded "local:tradingnews-key" guess broke autostart with
# SecretNotFound for anyone whose real key wasn't filed under exactly that
# name; canvas already had the correct value the whole time.


def _template_with_news_source(**config_overrides: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "test",
        "nodes": [
            {
                "id": "news-source-seed",
                "sectionType": "news_source",
                "position": {"x": 0, "y": 0},
                "config": config_overrides,
            }
        ],
        "edges": [],
    }


async def test_news_autostart_uses_canvas_saved_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openpoly.runtime.canvas_store import save_template

    save_template(
        _template_with_news_source(
            buffer_size=5000,
            freshness_seconds=600,
            urgency_filter="high",
            api_key_ref="env:REAL_KEY_FROM_CANVAS",
        )
    )

    captured: list[Any] = []

    async def fake_news_start(config: Any) -> None:
        captured.append(config)

    monkeypatch.setattr(news_manager, "start", fake_news_start)
    await _autostart_sources()

    assert len(captured) == 1
    cfg = captured[0]
    assert cfg["buffer_size"] == 5000  # from canvas, not the 1000 default
    assert cfg["freshness_seconds"] == 600  # from canvas, not the 1800 default
    assert cfg["urgency_filter"] == "high"  # from canvas, not "all"
    # api_key_ref is canvas-sourced too now — honored as-is, no override.
    assert cfg["api_key_ref"] == "env:REAL_KEY_FROM_CANVAS"


async def test_news_autostart_falls_back_to_defaults_when_no_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    async def fake_news_start(config: Any) -> None:
        captured.append(config)

    monkeypatch.setattr(news_manager, "start", fake_news_start)
    await _autostart_sources()

    assert len(captured) == 1
    cfg = captured[0]
    assert cfg["buffer_size"] == 1000
    assert cfg["freshness_seconds"] == 1800
    assert cfg["urgency_filter"] == "all"
    assert cfg["api_key_ref"] == "env:OPENPOLY_TRADINGNEWS_KEY"  # TradingNewsWSConfig's own default


async def test_news_autostart_respects_explicit_env_key_ref_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPENPOLY_NEWS_API_KEY_REF, when explicitly set, still wins over
    canvas — the escape hatch for remote deploys where the secret is
    already in process env and the operator would rather not touch the
    canvas config to match."""
    from openpoly.runtime.canvas_store import save_template

    save_template(_template_with_news_source(api_key_ref="env:REAL_KEY_FROM_CANVAS"))
    monkeypatch.setenv("OPENPOLY_NEWS_API_KEY_REF", "env:FROM_ENV_OVERRIDE")

    captured: list[Any] = []

    async def fake_news_start(config: Any) -> None:
        captured.append(config)

    monkeypatch.setattr(news_manager, "start", fake_news_start)
    await _autostart_sources()

    assert captured[0]["api_key_ref"] == "env:FROM_ENV_OVERRIDE"
