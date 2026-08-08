"""End-to-end regression test for the news-confluence bypass.

Reported bug: a heat-capped (or kill-switched / cooldown-locked) account
never recorded a news-confluence signal against an already-open position,
because ``EdgeThresholdEntryV0.run()`` returned ``skip("heat_cap")`` before
ever constructing an ``OrderIntent`` — so ``PaperExecutor.execute_buy``, where
the confluence-attach logic lives, was never called at all.

Unlike ``tests/test_section_entry_edge.py`` (entry section in isolation,
portfolio faked) and ``tests/test_executor.py`` (executor in isolation, no
entry section), this wires the REAL ``EdgeThresholdEntryV0`` to the REAL
``PaperExecutor`` over one real ``PortfolioStore`` — the same composition
``PipelineOrchestrator`` uses live — so a regression in either seam, or in how
they're wired together, shows up here even if each unit's own tests still
pass in isolation.
"""

from __future__ import annotations

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution import PaperExecutor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook, normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary
from openpoly.portfolio import PortfolioStore
from openpoly.sections._base import SectionInput
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry.edge_threshold_v0 import EdgeThresholdConfig, EdgeThresholdEntryV0


@pytest.fixture(autouse=True)
def _isolate_market_store():
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield
    market_source_manager.store = saved


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


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


def _book(token_id: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=[(bid, 100.0)], asks=[(ask, 100.0)])


def test_heat_capped_account_still_reinforces_an_existing_position(store) -> None:
    market = _market("m1")
    market_source_manager.store.replace(
        [market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    market_source_manager.store.set_order_books([_book("yes-m1", bid=0.68, ask=0.70)])

    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id="yes-m1",
        condition_id="0xm1",
        price=0.60,
        qty=16.7,
        ts=100.0,
        news_id="n1",
    )

    # heat_cap_usd is already tripped by the position just opened
    # (16.7 * 0.60 = $10.02 >= the $1 cap below) — on its own this would
    # skip the entry section before an OrderIntent ever exists.
    entry = EdgeThresholdEntryV0(
        EdgeThresholdConfig(heat_cap_usd=1.0),
        portfolio_provider=lambda: store,
    )
    out = entry.run(
        SectionInput(
            tick_type="event",
            payload=AnalysisResult(market_id="m1", p_model=0.75, confidence="high"),
        )
    )
    assert out.verdict == "ok", out.reason

    executor = PaperExecutor(store)
    result = executor.execute_buy(
        out.payload, news_id="n2", ts=200.0, p_model=0.75, confidence="high"
    )

    assert result.filled is False
    assert result.skip_reason == "position_exists"
    assert result.position_id == held.position_id
    # No second position opened — still exactly one open row on this market.
    assert store.get_open_positions() == [held]

    signals = store.signals_for_position(held.position_id)
    assert [s.relation for s in signals] == ["reinforce"]
    assert signals[0].news_id == "n2"
    assert signals[0].side == "yes"


def test_heat_capped_account_still_contradicts_an_existing_position(store) -> None:
    market = _market("m1")
    market_source_manager.store.replace(
        [market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={})
    )
    market_source_manager.store.set_order_books([_book("no-m1", bid=0.28, ask=0.30)])

    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id="yes-m1",
        condition_id="0xm1",
        price=0.60,
        qty=16.7,
        ts=100.0,
        news_id="n1",
    )

    entry = EdgeThresholdEntryV0(
        EdgeThresholdConfig(heat_cap_usd=1.0),
        portfolio_provider=lambda: store,
    )
    out = entry.run(
        SectionInput(
            tick_type="event",
            payload=AnalysisResult(market_id="m1", p_model=0.25, confidence="high"),
        )
    )
    assert out.verdict == "ok", out.reason
    assert out.payload.side == "no"

    executor = PaperExecutor(store)
    result = executor.execute_buy(
        out.payload, news_id="n3", ts=200.0, p_model=0.25, confidence="high"
    )

    assert result.filled is False
    assert result.skip_reason == "opposite_position_exists"
    assert result.position_id == held.position_id
    assert store.get_open_position("m1", "no") is None

    signals = store.signals_for_position(held.position_id)
    assert [s.relation for s in signals] == ["contradict"]
    assert signals[0].side == "no"
