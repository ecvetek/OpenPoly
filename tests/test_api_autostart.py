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
    # News autostart passes the full config dict with the local key ref.
    assert news_calls[0]["api_key_ref"] == "local:tradingnews-key"
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
