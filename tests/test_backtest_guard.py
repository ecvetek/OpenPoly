"""Tests for openpoly.backtest.guard — the flag coordinating a backtest
replay with the live orchestrator/exit monitor (which have no pause control
of their own — see guard.py's module docstring)."""

from __future__ import annotations

import pytest

from openpoly.backtest.guard import backtest_active, set_backtest_active


@pytest.fixture(autouse=True)
def _reset_guard():
    set_backtest_active(False)
    yield
    set_backtest_active(False)


def test_defaults_inactive() -> None:
    assert backtest_active() is False


def test_set_active_true_then_false() -> None:
    set_backtest_active(True)
    assert backtest_active() is True
    set_backtest_active(False)
    assert backtest_active() is False
