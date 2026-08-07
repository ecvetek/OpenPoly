"""Tests for ConfluenceExitV0 — the confluence-keyed peak-drawdown exit.

Mirrors the shape of ``test_section_exit_threshold.py``. The through-line is
one fixed mark (entry 0.50, peak 0.62, now 0.58 → a 33% retrace of the peak
gain) evaluated under different signal ledgers, so every difference in outcome
is attributable to the confluence state alone.
"""

from __future__ import annotations

from openpoly.portfolio.models import PositionSignal
from openpoly.sections._base import SectionInput
from openpoly.sections.exit.confluence_v0 import ConfluenceExitConfig, ConfluenceExitV0
from openpoly.sections.exit.threshold_v0 import CloseIntent, MarkedPosition

NOW = 100_000.0
HOUR = 3600.0


def _sig(
    sid: int,
    side: str,
    *,
    ts: float = NOW - 600,
    relation: str = "reinforce",
    confidence: str = "high",
) -> PositionSignal:
    return PositionSignal(
        id=sid,
        position_id=1,
        news_id=f"n{sid}",
        ts=ts,
        side=side,  # type: ignore[arg-type]
        relation=relation,  # type: ignore[arg-type]
        p_model=0.7,
        confidence=confidence,
    )


OPENING = _sig(1, "yes", ts=NOW - 900, relation="opening")
SECOND_YES = _sig(2, "yes", ts=NOW - 600)
A_NO = _sig(3, "no", ts=NOW - 300, relation="contradict")


def _retrace(*signals: PositionSignal, current: float = 0.58) -> MarkedPosition:
    """Entry 0.50, peak 0.62, mark ``current``. At 0.58 the retrace is
    (0.62-0.58)/(0.62-0.50) = 33% of the peak gain, and the return is +16% —
    under the 20% take-profit, so peak-drawdown is the only thing that can
    fire."""
    return MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=current,
        peak_price=0.62,
        news_signals=signals,
        marked_at=NOW,
    )


def _calm(*signals: PositionSignal) -> MarkedPosition:
    """Entry 0.50, peak 0.56, mark 0.555 — +11% return (under take-profit) and
    an 8% retrace (under even the tight contested threshold). Nothing fires on
    price alone, so anything that closes here closed for a confluence reason."""
    return MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=0.555,
        peak_price=0.56,
        news_signals=signals,
        marked_at=NOW,
    )


def _run(cfg: ConfluenceExitConfig, pos: MarkedPosition):
    return ConfluenceExitV0(cfg).run(SectionInput(tick_type="hard", payload=pos))


# ---------- guards ----------


def test_no_position_upstream_skips() -> None:
    out = _run(ConfluenceExitConfig(), None)  # type: ignore[arg-type]
    assert out.verdict == "skip"
    assert out.reason == "no position upstream"


def test_invalid_entry_price_skips() -> None:
    pos = MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.0,
        qty=20.0,
        current_price=0.58,
        peak_price=0.62,
        marked_at=NOW,
    )
    assert _run(ConfluenceExitConfig(), pos).verdict == "skip"


def test_no_signals_at_all_reads_as_solo() -> None:
    """A position opened before this feature shipped has an empty ledger —
    it must evaluate as solo, not crash and not silently disable drawdown."""
    out = _run(ConfluenceExitConfig(), _retrace())
    assert out.verdict == "ok"
    assert out.signals["confluence_state"] == "solo"


# ---------- state selects the threshold ----------


def test_solo_uses_solo_threshold() -> None:
    out = _run(ConfluenceExitConfig(), _retrace(OPENING))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "peak_drawdown"
    assert out.signals["confluence_state"] == "solo"
    assert out.signals["peak_drawdown_pct"] == 0.30
    assert out.signals["peak_dd"] == 0.3333


def test_solo_holds_when_retrace_is_under_its_threshold() -> None:
    """0.60 → a 17% retrace, under the 30% solo threshold. The same mark
    would have closed under the baseline section's 0.12 default — this is the
    'give a fresh position more room' half of the change."""
    out = _run(ConfluenceExitConfig(), _retrace(OPENING, current=0.60))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"


def test_reinforced_disables_peak_drawdown_by_default() -> None:
    out = _run(ConfluenceExitConfig(), _retrace(OPENING, SECOND_YES))
    assert out.verdict == "skip"
    assert out.signals["confluence_state"] == "reinforced"
    assert out.signals["peak_drawdown_pct"] is None


def test_reinforced_threshold_applies_when_configured() -> None:
    cfg = ConfluenceExitConfig(reinforced_peak_drawdown_pct=0.50)
    out = _run(cfg, _retrace(OPENING, SECOND_YES))
    assert out.verdict == "skip"  # 33% retrace is under the configured 50%
    cfg_tight = ConfluenceExitConfig(reinforced_peak_drawdown_pct=0.20)
    out_tight = _run(cfg_tight, _retrace(OPENING, SECOND_YES))
    assert out_tight.verdict == "ok"
    assert out_tight.payload.trigger == "peak_drawdown"


def test_contested_uses_the_tight_threshold() -> None:
    """Same 33% retrace as the reinforced case, opposite outcome — under the
    0.10 contested threshold it closes, where reinforced held."""
    out = _run(ConfluenceExitConfig(), _retrace(OPENING, SECOND_YES, A_NO))
    assert out.verdict == "ok"
    assert out.payload.trigger == "peak_drawdown"
    assert out.signals["confluence_state"] == "contested"
    assert out.signals["peak_drawdown_pct"] == 0.10
    assert out.signals["against"] == 1


def test_contested_closes_a_shallow_retrace_solo_would_hold() -> None:
    shallow = _retrace(OPENING, A_NO, current=0.60)  # 17% retrace
    assert _run(ConfluenceExitConfig(), shallow).verdict == "ok"
    assert _run(ConfluenceExitConfig(), _retrace(OPENING, current=0.60)).verdict == "skip"


# ---------- stop_loss is state-independent ----------


def test_stop_loss_fires_in_every_state() -> None:
    """The floor the confluence state must never widen. 0.40 vs a 0.50 entry
    is -20%, past the 15% stop."""
    for ledger in ((OPENING,), (OPENING, SECOND_YES), (OPENING, SECOND_YES, A_NO)):
        pos = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.40,
            peak_price=0.52,
            news_signals=ledger,
            marked_at=NOW,
        )
        out = _run(ConfluenceExitConfig(), pos)
        assert out.verdict == "ok", ledger
        assert out.payload.trigger == "stop_loss"


def test_take_profit_still_fires() -> None:
    pos = MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=0.65,
        peak_price=0.65,
        news_signals=(OPENING, SECOND_YES),
        marked_at=NOW,
    )
    out = _run(ConfluenceExitConfig(), pos)
    assert out.verdict == "ok"
    assert out.payload.trigger == "take_profit"


# ---------- contested_close_after ----------


def test_contested_close_after_is_off_by_default() -> None:
    """One contradiction on a position sitting comfortably inside every
    threshold holds — the default only tightens drawdown."""
    out = _run(ConfluenceExitConfig(), _calm(OPENING, A_NO))
    assert out.verdict == "skip"
    assert out.signals["confluence_state"] == "contested"


def test_contested_close_after_exits_at_the_configured_count() -> None:
    cfg = ConfluenceExitConfig(contested_close_after=2)
    assert _run(cfg, _calm(OPENING, A_NO)).verdict == "skip"  # one, need two

    out = _run(cfg, _calm(OPENING, A_NO, _sig(4, "no", ts=NOW - 100)))
    assert out.verdict == "ok"
    assert out.payload.trigger == "contested_exit"
    assert out.payload.qty == 20.0  # full close, not a partial


def test_contested_exit_fires_on_a_winning_position() -> None:
    """A broken thesis exits whether the position is up or down, so it
    outranks take-profit."""
    cfg = ConfluenceExitConfig(contested_close_after=1)
    winner = MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=0.65,  # +30%, past take_profit
        peak_price=0.65,
        news_signals=(OPENING, A_NO),
        marked_at=NOW,
    )
    out = _run(cfg, winner)
    assert out.payload.trigger == "contested_exit"


def test_stop_loss_still_outranks_contested_exit() -> None:
    cfg = ConfluenceExitConfig(contested_close_after=1)
    loser = MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=0.40,
        peak_price=0.52,
        news_signals=(OPENING, A_NO),
        marked_at=NOW,
    )
    assert _run(cfg, loser).payload.trigger == "stop_loss"


# ---------- config threading ----------


def test_support_ttl_reverts_a_stale_reinforcement() -> None:
    """The reinforcement is 3h old; with a 2h TTL the position is back to solo
    and its drawdown protection re-tightens accordingly."""
    stale = _sig(2, "yes", ts=NOW - 3 * HOUR)
    old_opening = _sig(1, "yes", ts=NOW - 4 * HOUR, relation="opening")
    cfg = ConfluenceExitConfig(support_ttl_minutes=120)
    out = _run(cfg, _retrace(old_opening, stale))
    assert out.signals["confluence_state"] == "solo"
    assert out.payload.trigger == "peak_drawdown"

    never = ConfluenceExitConfig(support_ttl_minutes=0)
    assert _run(never, _retrace(old_opening, stale)).verdict == "skip"


def test_min_signal_confidence_is_threaded_through() -> None:
    low = _sig(2, "yes", confidence="low")
    permissive = _run(ConfluenceExitConfig(), _retrace(OPENING, low))
    assert permissive.signals["confluence_state"] == "reinforced"

    strict = ConfluenceExitConfig(min_signal_confidence="medium")
    assert _run(strict, _retrace(OPENING, low)).signals["confluence_state"] == "solo"


def test_marked_at_is_the_only_clock() -> None:
    """Signals stamped after the mark do not count — the guard that keeps
    backtest replay honest, exercised through the section."""
    future = _sig(2, "yes", ts=NOW + 600)
    out = _run(ConfluenceExitConfig(), _retrace(OPENING, future))
    assert out.signals["confluence_state"] == "solo"
    assert out.signals["support"] == 1


def test_contract_test_passes() -> None:
    ConfluenceExitV0.CONTRACT_TEST()
