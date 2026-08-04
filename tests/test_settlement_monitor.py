"""Tests for SettlementMonitor — closes resolved-market positions at 0/1.

The Gamma fetch is faked so the test never hits the network. Portfolio is a
real one (sqlite tmp) so close_position math + status transitions are
exercised end-to-end on the real DB code path. The CloseReason "settlement"
must already be valid (Literal in portfolio.models).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.portfolio import PortfolioStore
from openpoly.runtime.section_log import SettlementDecision, settlement_log
from openpoly.runtime.settlement_monitor import (
    SettlementMonitor,
    _settlement_price_for_side,
)


@pytest.fixture(autouse=True)
def _reset_log():
    settlement_log.reset()
    yield
    settlement_log.reset()


@pytest.fixture
def store(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


def _raw_market(
    *,
    condition_id: str,
    closed: bool,
    outcome_prices: list[str] | None = None,
    yes_token: str | None = None,
    no_token: str | None = None,
    market_id: str = "m1",
    question: str = "Q?",
) -> dict[str, Any]:
    """Build a Gamma-shaped raw market dict — outcomePrices is the JSON
    string form Gamma actually returns."""
    yes = yes_token or f"yes-{condition_id}"
    no = no_token or f"no-{condition_id}"
    body: dict[str, Any] = {
        "id": market_id,
        "conditionId": condition_id,
        "question": question,
        "slug": "q",
        "clobTokenIds": json.dumps([yes, no]),
        "closed": closed,
        "acceptingOrders": not closed,
        "enableOrderBook": not closed,
        "negRisk": False,
        "lastTradePrice": 0.5,
        "endDate": "2026-06-01T00:00:00Z",
    }
    if outcome_prices is not None:
        body["outcomePrices"] = json.dumps(outcome_prices)
    return body


def _fetcher_returning(raw_markets: list[dict[str, Any]]):
    async def _fetch(condition_ids: list[str]) -> list[dict[str, Any]]:
        return [m for m in raw_markets if m.get("conditionId") in condition_ids]

    return _fetch


def _open_position(
    store: PortfolioStore,
    *,
    side: str = "yes",
    condition_id: str = "0xcid",
    market_id: str = "m1",
    avg: float = 0.40,
    qty: float = 10.0,
) -> int:
    token = f"{side}-{condition_id}"
    held = store.open_position(
        market_id=market_id,
        side=side,
        token_id=token,
        condition_id=condition_id,
        price=avg,
        qty=qty,
        ts=100.0,
        news_id="n",
    )
    return held.position_id


# ---------- _settlement_price_for_side ----------


def test_settlement_price_yes_wins_yes_side_returns_1() -> None:
    assert _settlement_price_for_side((1.0, 0.0), "yes") == 1.0


def test_settlement_price_yes_wins_no_side_returns_0() -> None:
    assert _settlement_price_for_side((1.0, 0.0), "no") == 0.0


def test_settlement_price_no_wins_no_side_returns_1() -> None:
    assert _settlement_price_for_side((0.0, 1.0), "no") == 1.0


def test_settlement_price_split_returns_none() -> None:
    """Ambiguous resolution (0.5/0.5) → caller must skip + retry later."""
    assert _settlement_price_for_side((0.5, 0.5), "yes") is None


def test_settlement_price_unresolved_floats_returns_none() -> None:
    """Mid-resolution snapshot (0.7/0.3) — not a clean 0/1 → skip."""
    assert _settlement_price_for_side((0.7, 0.3), "yes") is None


# ---------- _tick_once ----------


async def test_no_open_positions_is_noop(store) -> None:
    sm = SettlementMonitor(fetcher=_fetcher_returning([]))
    sm.configure(store)
    await sm._tick_once()
    assert settlement_log.entries() == []


# Steady-state outcomes are tick telemetry, not log entries. An unresolved
# market reports the same thing on every tick forever, so logging one entry per
# position per tick flooded the shared 200-entry ring (evicting real
# settlements and the reconciliation monitor's alerts) and grew
# settlement_decision by ~288 rows/position/day.


async def test_market_still_trading_counted_not_logged(store) -> None:
    pid = _open_position(store, condition_id="0xcid", side="yes")
    raw = [_raw_market(condition_id="0xcid", closed=False)]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    await sm._tick_once()
    rec = store.get_position(pid)
    assert rec is not None and rec.status == "open"
    assert settlement_log.entries() == []
    assert sm.last_tick["reason_counts"] == {"still_trading": 1}
    assert sm.last_tick["evaluated"] == 1
    assert sm.last_tick["settled"] == 0


async def test_repeated_ticks_do_not_grow_the_log(store) -> None:
    """The regression that motivated the change: an unresolved market must not
    accumulate a row per tick."""
    _open_position(store, condition_id="0xcid", side="yes")
    raw = [_raw_market(condition_id="0xcid", closed=False)]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    for _ in range(50):
        await sm._tick_once()
    assert settlement_log.entries() == []
    assert sm.last_tick["reason_counts"] == {"still_trading": 1}


async def test_market_closed_no_outcome_prices_counted(store) -> None:
    _open_position(store, condition_id="0xcid")
    raw = [_raw_market(condition_id="0xcid", closed=True, outcome_prices=None)]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    await sm._tick_once()
    assert settlement_log.entries() == []
    assert sm.last_tick["reason_counts"] == {"no_outcome_prices": 1}


async def test_ambiguous_outcome_counted(store) -> None:
    """Closed + outcomePrices=[0.5, 0.5] → skip (dispute / unresolved split)."""
    _open_position(store, condition_id="0xcid")
    raw = [
        _raw_market(
            condition_id="0xcid",
            closed=True,
            outcome_prices=["0.5", "0.5"],
        )
    ]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    await sm._tick_once()
    assert settlement_log.entries() == []
    assert sm.last_tick["reason_counts"] == {"ambiguous_outcome": 1}


async def test_yes_position_yes_wins_closes_at_1(store) -> None:
    pid = _open_position(store, condition_id="0xcid", side="yes", avg=0.40, qty=10.0)
    raw = [
        _raw_market(
            condition_id="0xcid",
            closed=True,
            outcome_prices=["1", "0"],
        )
    ]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    await sm._tick_once()
    rec = store.get_position(pid)
    assert rec is not None
    assert rec.status == "closed"
    assert rec.close_reason == "settlement"
    assert rec.realized_pnl == pytest.approx((1.0 - 0.40) * 10.0)
    entries = settlement_log.entries()
    assert entries[0].verdict == "ok"
    assert entries[0].final_price == 1.0
    assert entries[0].realized_pnl == pytest.approx(6.0)


async def test_no_position_yes_wins_closes_at_0(store) -> None:
    """Losing side: NO position when YES resolves → close at 0, full loss."""
    pid = _open_position(store, condition_id="0xcid", side="no", avg=0.30, qty=10.0)
    raw = [
        _raw_market(
            condition_id="0xcid",
            closed=True,
            outcome_prices=["1", "0"],
        )
    ]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    await sm._tick_once()
    rec = store.get_position(pid)
    assert rec is not None and rec.status == "closed"
    assert rec.realized_pnl == pytest.approx((0.0 - 0.30) * 10.0)
    entries = settlement_log.entries()
    assert entries[0].verdict == "ok"
    assert entries[0].final_price == 0.0


async def test_multiple_positions_same_market_all_close(store) -> None:
    """One Gamma fetch can resolve YES+NO positions on the same market."""
    pid_yes = _open_position(store, condition_id="0xcid", side="yes", avg=0.55, qty=5.0)
    pid_no = _open_position(store, condition_id="0xcid", side="no", avg=0.45, qty=5.0)
    raw = [
        _raw_market(
            condition_id="0xcid",
            closed=True,
            outcome_prices=["1", "0"],
        )
    ]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    await sm._tick_once()
    assert store.get_position(pid_yes).status == "closed"
    assert store.get_position(pid_no).status == "closed"
    assert {e.position_id for e in settlement_log.entries()} == {pid_yes, pid_no}


async def test_gamma_fetch_failure_surfaces_as_tick_error_no_crash(store) -> None:
    """Network exception in fetcher → loop must survive. The outage is one
    tick-level event plus a count, not a row per position for its duration."""
    _open_position(store, condition_id="0xcid")

    async def _boom(condition_ids: list[str]) -> list[dict]:
        raise ConnectionError("upstream unreachable")

    sm = SettlementMonitor(fetcher=_boom)
    sm.configure(store)
    await sm._tick_once()
    assert settlement_log.entries() == []
    assert sm.last_tick["reason_counts"] == {"gamma_fetch_failed": 1}
    events = sm.tick_events()
    assert events[-1]["kind"] == "tick_error"
    assert "gamma_fetch_failed" in (events[-1]["detail"] or "")


async def test_market_not_returned_by_gamma_counted(store) -> None:
    """Gamma returns partial list (our cid absent) → skip, don't close."""
    pid = _open_position(store, condition_id="0xcid")
    # Fetcher returns a different market's data
    raw = [_raw_market(condition_id="0xother", closed=True, outcome_prices=["1", "0"])]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    await sm._tick_once()
    rec = store.get_position(pid)
    assert rec is not None and rec.status == "open"
    assert settlement_log.entries() == []
    assert sm.last_tick["reason_counts"] == {"market_not_returned_by_gamma": 1}


# ---------- close-race handling ----------


async def test_already_closed_by_other_monitor_logs_skip_not_error(store) -> None:
    """close_position raising ValueError because something else (ExitMonitor,
    a manual close) already closed this position between the open-positions
    snapshot and this call is a benign race, not a real failure — must log
    skip, not error, and must not disturb the original close."""
    pid = _open_position(store, condition_id="0xcid", side="yes", avg=0.40, qty=10.0)

    async def _fetch_and_close_externally(condition_ids: list[str]) -> list[dict[str, Any]]:
        # Simulate ExitMonitor winning the race while this fetch was in flight.
        store.close_position(
            pid, sell_price=0.50, ts=150.0, close_reason="take_profit", trigger="take_profit"
        )
        return [_raw_market(condition_id="0xcid", closed=True, outcome_prices=["1", "0"])]

    sm = SettlementMonitor(fetcher=_fetch_and_close_externally)
    sm.configure(store)
    await sm._tick_once()

    entries = settlement_log.entries()
    assert entries[0].verdict == "skip"
    assert entries[0].reason == "already_closed_by_other_monitor"
    # The original close must be untouched — not overwritten by settlement.
    rec = store.get_position(pid)
    assert rec is not None
    assert rec.close_reason == "take_profit"


async def test_close_position_not_found_logs_error_not_skip() -> None:
    """A genuinely-missing position (never existed, not a race) must still
    log as a real error — never silently reclassified as a benign race."""
    from openpoly.portfolio import HeldPosition

    ghost = HeldPosition(
        position_id=99999,
        market_id="m1",
        side="yes",
        token_id="yes-0xcid",
        condition_id="0xcid",
        qty=10.0,
        avg_entry_price=0.40,
        opened_at=100.0,
    )

    class _GhostPortfolio:
        def get_open_positions(self) -> list[HeldPosition]:
            return [ghost]

        def close_position(self, *args: Any, **kwargs: Any) -> None:
            raise ValueError("position 99999 not found")

        def get_position(self, position_id: int) -> None:
            return None

    raw = [_raw_market(condition_id="0xcid", closed=True, outcome_prices=["1", "0"])]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(_GhostPortfolio())  # type: ignore[arg-type]
    await sm._tick_once()

    entries = settlement_log.entries()
    assert entries[0].verdict == "error"
    assert "close_failed" in (entries[0].error or "")


# ---------- persist hook ----------


async def test_persist_hook_fires_on_settlement(store) -> None:
    _open_position(store, condition_id="0xcid", side="yes", avg=0.40, qty=10.0)
    raw = [_raw_market(condition_id="0xcid", closed=True, outcome_prices=["1", "0"])]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    persisted: list[SettlementDecision] = []
    sm.set_settlement_persist(persisted.append)
    await sm._tick_once()
    assert [d.final_price for d in persisted] == [1.0]


async def test_raising_persist_hook_is_swallowed(store) -> None:
    _open_position(store, condition_id="0xcid", side="yes", avg=0.40, qty=10.0)
    raw = [_raw_market(condition_id="0xcid", closed=True, outcome_prices=["1", "0"])]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)

    def _boom(_decision) -> None:
        raise RuntimeError("persist boom")

    sm.set_settlement_persist(_boom)
    await sm._tick_once()  # must not raise
    assert len(settlement_log.entries()) == 1


async def test_persist_hook_cleared_via_none(store) -> None:
    _open_position(store, condition_id="0xcid", side="yes", avg=0.40, qty=10.0)
    raw = [_raw_market(condition_id="0xcid", closed=True, outcome_prices=["1", "0"])]
    sm = SettlementMonitor(fetcher=_fetcher_returning(raw))
    sm.configure(store)
    persisted: list[SettlementDecision] = []
    sm.set_settlement_persist(persisted.append)
    sm.set_settlement_persist(None)
    await sm._tick_once()
    assert persisted == []


async def test_tick_loop_start_stop(store) -> None:
    """start() spawns a task; stop() cancels cleanly."""
    sm = SettlementMonitor(
        fetcher=_fetcher_returning([]),
        tick_interval_seconds=3600,  # long; we won't wait for a tick
    )
    sm.configure(store)
    assert sm.state == "stopped"
    await sm.start()
    assert sm.state == "running"
    await asyncio.sleep(0)  # let the task spin up
    await sm.stop()
    assert sm.state == "stopped"


async def test_not_configured_is_noop() -> None:
    """tick before configure() → no DB access, no crash."""
    sm = SettlementMonitor(fetcher=_fetcher_returning([]))
    await sm._tick_once()
    assert settlement_log.entries() == []
