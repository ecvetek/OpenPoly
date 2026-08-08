"""Tests for ConvictionSizedEntryV0 — the confidence-tiered sizing entry section.

Mirrors tests/test_section_entry_edge.py's fixture pattern exactly (duplicated,
not imported — the section itself is a duplicate of EdgeThresholdEntryV0 by
design, see conviction_sized_v0.py's module docstring). Focuses on: the new
sizing behavior, a regression test for the shared-OrderIntent contract
(Risk #1 in the implementation plan), and one representative case per copied
gate to confirm the copy preserved behavior exactly.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass

import pytest

from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook, normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary
from openpoly.portfolio import HeldPosition, PositionRecord
from openpoly.sections._base import SectionInput
from openpoly.sections._registry import scan
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry import conviction_sized_v0, edge_threshold_v0
from openpoly.sections.entry.conviction_sized_v0 import (
    ConvictionSizedConfig,
    ConvictionSizedEntryV0,
)


@pytest.fixture(autouse=True)
def _isolate_market_store():
    """Each test gets a fresh market catalog singleton."""
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield
    market_source_manager.store = saved


def _market(market_id: str = "m1", *, clob: str | None = None):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Will X happen?",
        "clobTokenIds": clob or f'["yes-{market_id}", "no-{market_id}"]',
    }
    m = normalize_gamma_market(raw, event={"id": "e1", "title": "E", "tags": []})
    assert m is not None
    return m


def _book(token_id: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=[(bid, 100.0)], asks=[(ask, 100.0)])


def _populate(market, *books: OrderBook) -> None:
    store = market_source_manager.store
    store.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
    store.set_order_books(list(books))


def _ar(market_id: str = "m1", p_model: float = 0.7, confidence: str = "medium") -> AnalysisResult:
    return AnalysisResult(market_id=market_id, p_model=p_model, confidence=confidence)


def _run(inst: ConvictionSizedEntryV0, payload: object):
    return inst.run(SectionInput(tick_type="event", payload=payload))


# ---------- catalog / contract ----------


def test_conviction_sized_in_default_catalog() -> None:
    matches = [e for e in scan() if e.name == "ConvictionSizedEntryV0"]
    assert len(matches) == 1
    assert matches[0].type == "entry"


def test_order_intent_is_the_shared_edge_threshold_class() -> None:
    """Regression test for Risk #1: the payload MUST be an instance of
    edge_threshold_v0.OrderIntent, not a locally-redefined lookalike — the
    orchestrator's isinstance check is keyed on that exact class, and a
    same-named-but-distinct class here would make every entry a silent
    no-op (verdict 'ok', executor never called)."""
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    out = _run(ConvictionSizedEntryV0(ConvictionSizedConfig()), _ar(p_model=0.7))
    assert out.verdict == "ok"
    assert isinstance(out.payload, edge_threshold_v0.OrderIntent)
    assert conviction_sized_v0.OrderIntent is edge_threshold_v0.OrderIntent


def test_no_analysis_skips() -> None:
    out = _run(ConvictionSizedEntryV0(ConvictionSizedConfig()), None)
    assert out.verdict == "skip"


# ---------- confidence-tiered sizing ----------


@pytest.mark.parametrize(
    "confidence,multiplier_field,expected_multiplier",
    [
        ("low", "low_multiplier", 0.5),
        ("medium", "medium_multiplier", 1.0),
        ("high", "high_multiplier", 1.5),
    ],
)
def test_qty_scales_by_confidence_tier(
    confidence: str, multiplier_field: str, expected_multiplier: float
) -> None:
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    inst = ConvictionSizedEntryV0(ConvictionSizedConfig(order_size_usd=20.0))
    out = _run(inst, _ar(p_model=0.7, confidence=confidence))
    assert out.verdict == "ok"
    assert out.payload.qty == pytest.approx(20.0 * expected_multiplier / 0.42)
    assert out.signals["confidence"] == confidence
    assert out.signals["size_multiplier"] == pytest.approx(expected_multiplier)


def test_custom_multipliers_are_respected() -> None:
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    inst = ConvictionSizedEntryV0(ConvictionSizedConfig(order_size_usd=10.0, high_multiplier=2.5))
    out = _run(inst, _ar(p_model=0.7, confidence="high"))
    assert out.verdict == "ok"
    assert out.payload.qty == pytest.approx(10.0 * 2.5 / 0.42)


def test_out_of_range_confidence_falls_back_to_medium_multiplier() -> None:
    """AnalysisResult is a plain dataclass — Confidence's Literal type isn't
    runtime-enforced, so a non-conforming third-party analyzer (anything
    other than today's built-in llm_v0, which does validate) can hand this
    section a confidence value outside "low"/"medium"/"high". Must not
    KeyError; falls back to the medium multiplier."""
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    inst = ConvictionSizedEntryV0(ConvictionSizedConfig(order_size_usd=20.0))
    out = _run(inst, _ar(p_model=0.7, confidence="very_high"))
    assert out.verdict == "ok"
    assert out.payload.qty == pytest.approx(20.0 * 1.0 / 0.42)
    assert out.signals["size_multiplier"] == pytest.approx(1.0)


# ---------- edge / spread gates (copied logic, one case each) ----------


def test_edge_below_min_edge_skips() -> None:
    _populate(_market(), _book("yes-m1", bid=0.66, ask=0.68))
    out = _run(ConvictionSizedEntryV0(ConvictionSizedConfig()), _ar(p_model=0.70))
    assert out.verdict == "skip"
    assert out.reason == "edge below min_edge"


def test_spread_above_max_spread_skips() -> None:
    _populate(_market(), _book("yes-m1", bid=0.22, ask=0.42))
    out = _run(ConvictionSizedEntryV0(ConvictionSizedConfig()), _ar(p_model=0.70))
    assert out.verdict == "skip"
    assert out.reason == "spread above max_spread"


def test_side_lock_blocks_no() -> None:
    inst = ConvictionSizedEntryV0(ConvictionSizedConfig(side_lock=True))
    out = _run(inst, _ar(p_model=0.3))
    assert out.verdict == "skip"
    assert out.reason == "side_lock active"


# ---------- late-buy veto (copied logic) ----------


def test_veto_disabled_by_default_does_not_consult_recent_move(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        conviction_sized_v0,
        "recent_move",
        lambda token_id, *, window_min: calls.append(token_id) or 0.99,
    )
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    out = _run(ConvictionSizedEntryV0(ConvictionSizedConfig()), _ar(p_model=0.7))
    assert out.verdict == "ok"
    assert calls == []


def test_veto_skips_on_large_move(monkeypatch) -> None:
    monkeypatch.setattr(conviction_sized_v0, "recent_move", lambda token_id, *, window_min: 0.15)
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    inst = ConvictionSizedEntryV0(ConvictionSizedConfig(veto_enabled=True))
    out = _run(inst, _ar(p_model=0.7))
    assert out.verdict == "skip"
    assert out.reason == "late buy"


# ---------- same_market_cooldown / lifetime_lockout (copied logic) ----------


@dataclass
class _FakePortfolio:
    records: list[PositionRecord]

    def list_positions(self, limit: int = 100) -> list[PositionRecord]:
        return list(self.records[:limit])

    def get_open_positions(self) -> list[HeldPosition]:
        return [
            HeldPosition(
                position_id=r.id,
                market_id=r.market_id,
                side=r.side,
                token_id=r.token_id,
                condition_id=r.condition_id,
                qty=r.qty,
                avg_entry_price=r.avg_entry_price,
                opened_at=r.opened_at,
            )
            for r in self.records
            if r.status == "open"
        ]

    def get_open_position(self, market_id: str, side: str) -> HeldPosition | None:
        # The news-confluence bypass: matches an open position on
        # (market_id, side) exactly, same contract as PortfolioStore's own
        # method.
        for h in self.get_open_positions():
            if h.market_id == market_id and h.side == side:
                return h
        return None


def _rec(
    market_id: str,
    side: str,
    opened_at: float,
    *,
    closed_at: float | None = None,
    position_id: int = 1,
    realized_pnl: float = -1.50,
) -> PositionRecord:
    return PositionRecord(
        id=position_id,
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        token_id=f"{side}-{market_id}",
        condition_id=f"0x{market_id}",
        qty=10.0,
        avg_entry_price=0.40,
        status="closed" if closed_at else "open",
        opened_at=opened_at,
        closed_at=closed_at,
        close_reason="stop_loss" if closed_at else None,
        realized_pnl=realized_pnl if closed_at else None,
    )


def test_cooldown_blocks_recent_close_same_market_side() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    now = _time.time()
    recent_closed = _rec("m1", "no", opened_at=now - 30 * 60, closed_at=now - 10 * 60)
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(same_market_cooldown_minutes=30),
        portfolio_provider=lambda: _FakePortfolio([recent_closed]),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "same_market_cooldown"


def test_lockout_blocks_any_prior_position_no_matter_how_old() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    very_old = _rec(
        "m1", "no", opened_at=_time.time() - 99 * 3600, closed_at=_time.time() - 98 * 3600
    )
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(same_market_lifetime_lockout=True),
        portfolio_provider=lambda: _FakePortfolio([very_old]),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "same_market_lockout"


# ---------- heat_cap_usd (copied logic) ----------


def test_heat_cap_blocks_when_open_cost_at_or_above_cap() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    opens = [_rec("other1", "yes", opened_at=_time.time() - 600, position_id=10)]
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(heat_cap_usd=1.0),
        portfolio_provider=lambda: _FakePortfolio(opens),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "heat_cap"


# ---------- A4 kill switch (copied logic, one case each) ----------


def test_kill_consecutive_losses_blocks_after_threshold() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    now = _time.time()
    records = [
        _rec(f"m{i}", "yes", opened_at=now - i * 700, closed_at=now - i * 600, position_id=10 + i)
        for i in range(1, 6)
    ]
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(kill_max_consecutive_losses=5),
        portfolio_provider=lambda: _FakePortfolio(records),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "kill_consecutive_losses"


def test_kill_daily_loss_blocks_when_24h_sum_exceeds_cap() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    now = _time.time()
    records = [
        _rec(
            f"m{i}",
            "yes",
            opened_at=now - i * 3700,
            closed_at=now - i * 3600,
            position_id=10 + i,
            realized_pnl=-4.0,
        )
        for i in range(1, 4)
    ]
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(kill_daily_loss_usd=10.0),
        portfolio_provider=lambda: _FakePortfolio(records),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "kill_daily_loss"


def test_kill_drawdown_blocks_on_peak_to_trough_drop() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    now = _time.time()
    records = [
        _rec(
            "m4", "yes", opened_at=now - 150, closed_at=now - 100, position_id=4, realized_pnl=-3.0
        ),
        _rec(
            "m3", "yes", opened_at=now - 250, closed_at=now - 200, position_id=3, realized_pnl=-1.0
        ),
        _rec(
            "m2", "yes", opened_at=now - 350, closed_at=now - 300, position_id=2, realized_pnl=+2.0
        ),
        _rec(
            "m1", "yes", opened_at=now - 450, closed_at=now - 400, position_id=1, realized_pnl=+3.0
        ),
    ]
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(kill_max_drawdown_usd=3.0),
        portfolio_provider=lambda: _FakePortfolio(records),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "kill_drawdown"


def test_kill_switch_disabled_by_default_no_portfolio_touch() -> None:
    """All gates 0/False (the default Config) → provider never invoked even
    when supplied — preserves the 'no portfolio touch on default' contract."""
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    called = {"count": 0}

    def provider() -> _FakePortfolio:
        called["count"] += 1
        return _FakePortfolio(
            [
                _rec(
                    "m1",
                    "yes",
                    opened_at=_time.time() - 100,
                    closed_at=_time.time() - 50,
                    realized_pnl=-1000.0,
                )
            ]
        )

    inst = ConvictionSizedEntryV0(ConvictionSizedConfig(), portfolio_provider=provider)
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "ok"
    assert called["count"] == 0


# ---------- news-confluence bypass (copied logic, one case each) ----------
#
# See test_section_entry_edge.py's full matrix for the rationale — this file
# only mirrors it, one representative case per gate, to confirm the copy
# preserved this behavior exactly (same convention this file already uses
# for edge/spread/cooldown/heat_cap/kill-switch above).


def test_side_lock_bypassed_when_position_exists_on_this_market() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    existing = _rec("m1", "no", opened_at=_time.time() - 60)
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(side_lock=True),
        portfolio_provider=lambda: _FakePortfolio([existing]),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "ok"
    assert out.payload.side == "no"


def test_heat_cap_bypassed_when_opposite_side_position_already_open() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    existing = _rec("m1", "yes", opened_at=_time.time() - 60)
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(heat_cap_usd=1.0),
        portfolio_provider=lambda: _FakePortfolio([existing]),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "ok"


def test_kill_switch_bypassed_when_position_exists_on_this_market() -> None:
    existing = _rec("m1", "no", opened_at=_time.time() - 60)
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(kill_max_consecutive_losses=1),
        portfolio_provider=lambda: _FakePortfolio([existing]),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "ok"


def test_cooldown_bypassed_when_the_open_position_is_the_intended_side() -> None:
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    existing = _rec("m1", "no", opened_at=_time.time() - 60)
    inst = ConvictionSizedEntryV0(
        ConvictionSizedConfig(same_market_cooldown_minutes=30),
        portfolio_provider=lambda: _FakePortfolio([existing]),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "ok"
