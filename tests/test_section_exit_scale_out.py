"""Tests for ScaleOutExitV0 — partial-profit-then-raised-stop exit section.

Mirrors tests/test_section_exit_threshold.py's ``_pos()`` helper pattern.
Covers both rule sets (pre- and post-scale-out), the CloseIntent.qty split
between partial and full-close branches, and a regression test for the
shared-class contract (Risk #1 in the implementation plan) since this
section imports MarkedPosition/CloseIntent from threshold_v0 rather than
redefining them.
"""

from __future__ import annotations

from openpoly.sections._base import SectionInput
from openpoly.sections._registry import scan
from openpoly.sections.exit import threshold_v0
from openpoly.sections.exit.scale_out_v0 import ScaleOutExitConfig, ScaleOutExitV0
from openpoly.sections.exit.threshold_v0 import CloseIntent, MarkedPosition


def _pos(
    current_price: float,
    avg_entry_price: float = 0.50,
    *,
    peak_price: float | None = None,
    qty: float = 20.0,
    scaled_out: bool = False,
) -> MarkedPosition:
    return MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=avg_entry_price,
        qty=qty,
        current_price=current_price,
        peak_price=current_price if peak_price is None else peak_price,
        scaled_out=scaled_out,
    )


def _run(inst: ScaleOutExitV0, pos: MarkedPosition | None):
    return inst.run(SectionInput(tick_type="hard", payload=pos))


# ---------- catalog / contract ----------


def test_scale_out_in_default_catalog() -> None:
    matches = [e for e in scan() if e.name == "ScaleOutExitV0"]
    assert len(matches) == 1
    assert matches[0].type == "exit"


def test_close_intent_is_the_shared_threshold_class() -> None:
    """Regression test for Risk #1: the payload MUST be an instance of
    threshold_v0.CloseIntent — ExitMonitor's isinstance check is keyed on
    that exact class, and it also constructs threshold_v0.MarkedPosition
    directly, so a same-named local class here would make every close a
    silent no-op (the monitor would log 'no position upstream' forever)."""
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    out = _run(inst, _pos(0.65))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)


def test_run_no_position_skips() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    out = _run(inst, None)
    assert out.verdict == "skip"
    assert out.reason == "no position upstream"


def test_invalid_entry_price_skips() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    out = _run(inst, _pos(0.5, avg_entry_price=0.0))
    assert out.verdict == "skip"
    assert out.reason == "invalid avg_entry_price"


# ---------- pre-scale-out branch ----------


def test_pre_scale_out_within_thresholds_holds() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    out = _run(inst, _pos(0.52))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"
    assert out.signals["scaled_out"] is False


def test_pre_scale_out_stop_loss_closes_full_qty() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    out = _run(inst, _pos(0.40, qty=20.0))  # -20% ≤ -15% stop_loss_pct
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "stop_loss"
    assert out.payload.qty == 20.0


def test_pre_scale_out_peak_drawdown_beats_scale_out() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    # Peak 0.80 (+60%), now 0.60 (+20%, exactly at scale_out_trigger_pct).
    # peak_dd = (0.80-0.60)/(0.80-0.50) = 0.667 ≥ 0.12 → peak_drawdown wins.
    out = _run(inst, _pos(0.60, peak_price=0.80, qty=20.0))
    assert out.verdict == "ok"
    assert out.payload.trigger == "peak_drawdown"
    assert out.payload.qty == 20.0  # full close, not partial


def test_scale_out_sells_only_the_configured_fraction() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig(scale_out_fraction=0.5))
    out = _run(inst, _pos(0.65, qty=20.0))  # +30% ≥ 20% scale_out_trigger_pct
    assert out.verdict == "ok"
    assert out.payload.trigger == "scale_out"
    assert out.payload.qty == 10.0  # 20 * 0.5
    assert out.signals["scaled_out"] is False  # section is stateless — monitor sets this


def test_scale_out_fraction_is_configurable() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig(scale_out_fraction=0.25))
    out = _run(inst, _pos(0.65, qty=20.0))
    assert out.payload.qty == 5.0  # 20 * 0.25


# ---------- post-scale-out branch ----------


def test_post_scale_out_within_thresholds_holds() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    # Remainder at breakeven-ish return, no peak retrace, no final target hit.
    out = _run(inst, _pos(0.505, qty=10.0, scaled_out=True))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"


def test_post_scale_out_stop_closes_at_breakeven_by_default() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    # -2% ≤ post_scale_out_stop_pct (0.0 = breakeven) → closes the remainder.
    out = _run(inst, _pos(0.49, qty=10.0, peak_price=0.65, scaled_out=True))
    assert out.verdict == "ok"
    assert out.payload.trigger == "post_scale_out_stop"
    assert out.payload.qty == 10.0  # full remainder, not another partial


def test_post_scale_out_stop_level_is_configurable() -> None:
    # A custom (looser) post-scale-out stop at -10%. No peak set above entry
    # (peak defaults to current_price) so peak_drawdown can't confound this —
    # it isolates the stop-level behavior specifically.
    inst = ScaleOutExitV0(ScaleOutExitConfig(post_scale_out_stop_pct=-0.10))
    out = _run(inst, _pos(0.49, qty=10.0, scaled_out=True))  # -2%
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"

    out2 = _run(inst, _pos(0.44, qty=10.0, scaled_out=True))  # -12%
    assert out2.verdict == "ok"
    assert out2.payload.trigger == "post_scale_out_stop"


def test_post_scale_out_peak_drawdown_still_applies() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    # Remainder ran to 0.90 (+80%), retraced to 0.70 (+40%, above breakeven
    # so post_scale_out_stop doesn't fire). peak_dd = (0.90-0.70)/(0.90-0.50)
    # = 0.5 ≥ 0.12 → peak_drawdown closes the remainder.
    out = _run(inst, _pos(0.70, qty=10.0, peak_price=0.90, scaled_out=True))
    assert out.verdict == "ok"
    assert out.payload.trigger == "peak_drawdown"
    assert out.payload.qty == 10.0


def test_final_take_profit_disabled_by_default_never_fires() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    # +50% return on the remainder — with final_take_profit_pct=None, this
    # must NOT close (no second target configured).
    out = _run(inst, _pos(0.75, qty=10.0, scaled_out=True))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"


def test_final_take_profit_closes_remainder_when_configured() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig(final_take_profit_pct=0.40))
    out = _run(inst, _pos(0.75, qty=10.0, scaled_out=True))  # +50% ≥ 40%
    assert out.verdict == "ok"
    assert out.payload.trigger == "final_take_profit"
    assert out.payload.qty == 10.0


# ---------- shared peak-tracking logic parity with the baseline ----------


def test_peak_meaningful_floor_still_applies() -> None:
    inst = ScaleOutExitV0(ScaleOutExitConfig())
    # qty 2 so even a +30% peak only banks $0.30 < $1 default USD floor.
    out = _run(inst, _pos(0.55, peak_price=0.65, qty=2.0))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"
    assert out.signals["peak_meaningful"] is False


def test_threshold_v0_marked_position_scaled_out_defaults_false() -> None:
    """threshold_v0.MarkedPosition's new field defaults False and is a no-op
    for the baseline ThresholdExitV0 — additive-edit regression check."""
    pos = threshold_v0.MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=0.52,
        peak_price=0.52,
    )
    assert pos.scaled_out is False
    baseline = threshold_v0.ThresholdExitV0(threshold_v0.ThresholdExitConfig())
    out = baseline.run(SectionInput(tick_type="hard", payload=pos))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"
