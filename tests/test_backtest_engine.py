"""End-to-end tests for the backtest engine — seeds a temp DB with persisted
analyzer calls + order-book snapshots, runs run_backtest, and checks the
resulting P&L against a hand-computed scripted scenario.
"""

from __future__ import annotations

import json

import pytest

from openpoly.backtest import engine as engine_module
from openpoly.backtest.engine import BacktestRequest, run_backtest
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.tables import AnalyzerCallRow, OrderBookSnapshot
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary
from openpoly.sections.exit.confluence_v0 import ConfluenceExitConfig, ConfluenceExitV0
from openpoly.sections.exit.threshold_v0 import ThresholdExitConfig, ThresholdExitV0


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


@pytest.fixture(autouse=True)
def _isolate_market_store():
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield
    market_source_manager.store = saved


@pytest.fixture
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/backtest.db")
    init_db(engine)
    return make_session_factory(engine)


def _seed_analyzer_call(sf, **overrides) -> None:
    defaults = dict(
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
    defaults.update(overrides)
    with sf() as session:
        session.add(AnalyzerCallRow(**defaults))
        session.commit()


def _seed_book(sf, token_id: str, ts: float, bid: float, ask: float) -> None:
    with sf() as session:
        session.add(
            OrderBookSnapshot(
                token_id=token_id,
                recorded_at=ts,
                bids_json=json.dumps([[bid, 100.0]]),
                asks_json=json.dumps([[ask, 100.0]]),
            )
        )
        session.commit()


def _default_request(**overrides) -> BacktestRequest:
    defaults = dict(
        since=0.0,
        until=200.0,
        entry_module="openpoly.sections.entry.edge_threshold_v0",
        entry_name="EdgeThresholdEntryV0",
        entry_config={},
        exit_module="openpoly.sections.exit.threshold_v0",
        exit_name="ThresholdExitV0",
        exit_config={},
    )
    defaults.update(overrides)
    return BacktestRequest(**defaults)


def test_backtest_replays_entry_and_exit_matching_hand_computed_pnl(sf) -> None:
    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    _seed_analyzer_call(sf, ts=100.0, market_id="m1", p_model=0.7, confidence="high")
    # Entry snapshot: ask 0.42 -> edge (0.7 - 0.42) = 0.28 >= min_edge 0.05.
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)
    # Exit snapshot: bid climbs to 0.55 -> return (0.55-0.42)/0.42 = 0.31 >= take_profit 0.20.
    _seed_book(sf, "yes-m1", ts=150.0, bid=0.55, ask=0.57)

    result = run_backtest(_default_request(), sf)

    assert result.replayed_analyzer_calls == 1
    assert result.skipped_market_not_in_catalog == 0

    expected_qty = 10.0 / 0.42  # default order_size_usd=10
    expected_pnl = (0.55 - 0.42) * expected_qty

    summary = result.statistics.summary
    assert summary.positions_closed == 1
    assert summary.wins == 1
    assert summary.losses == 0
    assert summary.net_pnl == pytest.approx(expected_pnl)
    assert result.statistics.closed_positions[0].close_reason == "take_profit"
    assert result.statistics.closed_positions[0].qty == pytest.approx(expected_qty)


def test_backtest_counts_skipped_market_not_in_catalog(sf) -> None:
    # Live catalog is empty (no markets registered) — the analyzer call's
    # market_id can't be resolved.
    _seed_analyzer_call(sf, ts=100.0, market_id="m1")
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)

    result = run_backtest(_default_request(), sf)

    assert result.replayed_analyzer_calls == 0
    assert result.skipped_market_not_in_catalog == 1
    assert result.statistics.summary.positions_closed == 0


def test_backtest_ignores_non_ok_verdict_analyzer_calls(sf) -> None:
    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    _seed_analyzer_call(sf, ts=100.0, verdict="skip", p_model=None, confidence=None)
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)

    result = run_backtest(_default_request(), sf)

    assert result.replayed_analyzer_calls == 0
    assert result.statistics.summary.positions_closed == 0


def test_backtest_restores_market_store_even_on_exception(sf, monkeypatch) -> None:
    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    _seed_analyzer_call(sf, ts=100.0)
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)
    original_store = market_source_manager.store

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-replay")

    monkeypatch.setattr(engine_module, "order_book_snapshots_for_token", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        run_backtest(_default_request(), sf)

    assert market_source_manager.store is original_store


def test_backtest_restores_guard_even_on_exception(sf, monkeypatch) -> None:
    from openpoly.backtest.guard import backtest_active

    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    _seed_analyzer_call(sf, ts=100.0)
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-replay")

    monkeypatch.setattr(engine_module, "order_book_snapshots_for_token", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        run_backtest(_default_request(), sf)

    assert backtest_active() is False


def test_backtest_raises_when_already_active(sf) -> None:
    from openpoly.backtest.guard import set_backtest_active

    set_backtest_active(True)
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            run_backtest(_default_request(), sf)
    finally:
        set_backtest_active(False)


def test_backtest_leaves_position_open_when_no_exit_trigger_fires(sf) -> None:
    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    _seed_analyzer_call(sf, ts=100.0)
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)
    # Small move, well within thresholds — nothing closes.
    _seed_book(sf, "yes-m1", ts=150.0, bid=0.41, ask=0.43)

    result = run_backtest(_default_request(), sf)

    assert result.statistics.summary.positions_closed == 0
    assert result.statistics.summary.positions_opened == 1


# ---------- news confluence in replay ----------


def test_replay_accumulates_the_confluence_ledger(sf) -> None:
    """A second same-side analyzer call and a later opposite-side one both
    attach to the one open position instead of opening new ones — exactly what
    happens live, so ConfluenceExitV0 can be evaluated against real history."""
    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    _seed_analyzer_call(sf, ts=100.0, news_id="n1", p_model=0.7, confidence="high")
    _seed_analyzer_call(sf, ts=110.0, news_id="n2", p_model=0.8, confidence="high")
    _seed_analyzer_call(sf, ts=120.0, news_id="n3", p_model=0.1, confidence="high")
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)
    _seed_book(sf, "no-m1", ts=120.0, bid=0.40, ask=0.42)

    captured: list = []

    class _Recorder:
        """Wraps the real exit section so we can read the MarkedPosition it
        was handed at each replayed snapshot."""

        Config = ThresholdExitConfig

        def __init__(self, config) -> None:
            self._inner = ThresholdExitV0(config)

        def run(self, section_input):
            captured.append(section_input.payload)
            return self._inner.run(section_input)

    monkeypatched = {("exit", "m", "Recorder"): _Recorder}
    orig_resolve = engine_module.resolve_impl

    def _resolve(section_type, module, name):
        cls = monkeypatched.get((section_type, module, name))
        return cls if cls is not None else orig_resolve(section_type, module, name)

    engine_module.resolve_impl = _resolve
    try:
        run_backtest(
            _default_request(
                until=200.0,
                exit_module="m",
                exit_name="Recorder",
                exit_config={"take_profit_pct": 10.0, "stop_loss_pct": 1.0},
            ),
            sf,
        )
    finally:
        engine_module.resolve_impl = orig_resolve

    assert captured, "exit section was never evaluated"
    ledger = captured[-1].news_signals
    assert [s.relation for s in ledger] == ["opening", "reinforce", "contradict"]
    assert [s.news_id for s in ledger] == ["n1", "n2", "n3"]
    assert [s.side for s in ledger] == ["yes", "yes", "no"]
    assert ledger[2].p_model == 0.1  # snapshotted from the analyzer row


def test_replay_never_lets_a_later_signal_steer_an_earlier_mark(sf) -> None:
    """Entries are ALL replayed before any exit tick, so the ledger handed to
    the section contains signals from the position's future. ``marked_at`` is
    the snapshot's own recorded_at and ``confluence.evaluate`` drops anything
    stamped after it — without that the confluence rules would look good for
    entirely fake reasons."""
    market_source_manager.store.replace(
        [_market("m1")], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    _seed_analyzer_call(sf, ts=100.0, news_id="n1", p_model=0.7, confidence="high")
    _seed_analyzer_call(sf, ts=180.0, news_id="n2", p_model=0.8, confidence="high")
    _seed_book(sf, "yes-m1", ts=100.0, bid=0.40, ask=0.42)
    _seed_book(sf, "yes-m1", ts=120.0, bid=0.43, ask=0.45)  # before the 2nd call
    _seed_book(sf, "yes-m1", ts=190.0, bid=0.44, ask=0.46)  # after it

    states: list[tuple[float, str]] = []

    class _StateRecorder:
        Config = ConfluenceExitConfig

        def __init__(self, config) -> None:
            self._inner = ConfluenceExitV0(config)

        def run(self, section_input):
            out = self._inner.run(section_input)
            states.append((section_input.payload.marked_at, out.signals["confluence_state"]))
            return out

    orig_resolve = engine_module.resolve_impl

    def _resolve(section_type, module, name):
        if name == "StateRecorder":
            return _StateRecorder
        return orig_resolve(section_type, module, name)

    engine_module.resolve_impl = _resolve
    try:
        run_backtest(
            _default_request(
                until=300.0,
                exit_module="m",
                exit_name="StateRecorder",
                exit_config={"take_profit_pct": 10.0, "stop_loss_pct": 1.0},
            ),
            sf,
        )
    finally:
        engine_module.resolve_impl = orig_resolve

    by_ts = dict(states)
    assert by_ts[120.0] == "solo"  # the 2nd headline hasn't happened yet
    assert by_ts[190.0] == "reinforced"  # by now it has
