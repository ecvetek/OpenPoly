"""Scale-out exit — take a partial profit at the first target, let the
remainder ride with a raised stop instead of exiting the whole position.

Targets the pattern the README's own live-window writeup calls out as the
strategy's weakest link: post-entry peaks of +67%/+142%/+161% booked for a
few dollars because the baseline ``ThresholdExitV0`` closes the *entire*
position at a single take-profit threshold. Here, hitting
``scale_out_trigger_pct`` sells only ``scale_out_fraction`` of the position;
the remainder's stop moves up to ``post_scale_out_stop_pct`` (0.0 = breakeven
by default) instead of the original (tighter) ``stop_loss_pct``, and an
optional ``final_take_profit_pct`` can close what's left at a second, higher
target.

Two rule sets, chosen by ``pos.scaled_out`` (injected by ``ExitMonitor`` —
see ``MarkedPosition.scaled_out`` in ``threshold_v0.py``; this section itself
stays a pure function of its input, no cross-tick state of its own, same
discipline as the baseline)::

    not yet scaled out:  stop_loss → peak_drawdown → scale_out (PARTIAL)
    already scaled out:  post_scale_out_stop → peak_drawdown → final_take_profit

``post_scale_out_stop_pct`` is a fixed level, not a trailing stop —
``threshold_v0.py``'s own docstring notes a trailing-stop variant (with
cost-adjusted peaks) was already considered and deliberately rejected for the
baseline as more parameters than the data can justify; this keeps the same
discipline rather than reopening that debate.

Imports ``MarkedPosition``, ``CloseIntent`` and ``Trigger`` from
``threshold_v0`` rather than redefining them — ``ExitMonitor`` constructs the
former directly and ``isinstance``-checks the latter, so a same-named local
class here would make every close a silent no-op (the monitor would log
"no position upstream" forever). See that module's docstring / the
implementation plan's Risk #1 for the full reasoning.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.exit.threshold_v0 import CloseIntent, MarkedPosition, Trigger


class ScaleOutExitConfig(BaseModel):
    stop_loss_pct: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Pre-scale-out only. Close the position when its loss reaches this fraction.",
    )
    peak_drawdown_pct: float = Field(
        default=0.12,
        ge=0.0,
        le=1.0,
        description="Applies in both phases. Close when the gain has retraced this fraction from peak.",
    )
    peak_meaningful_floor_usd: float = Field(
        default=1.0,
        ge=0.0,
        description="Skip peak_drawdown unless the peak gain in USD exceeds this floor.",
    )
    peak_meaningful_floor_pct: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description="Skip peak_drawdown unless the peak gain exceeds this fraction of cost basis.",
    )
    scale_out_fraction: float = Field(
        default=0.5,
        ge=0.05,
        le=0.95,
        description="Fraction of qty sold when scale_out_trigger_pct first fires.",
    )
    scale_out_trigger_pct: float = Field(
        default=0.20,
        ge=0.0,
        le=10.0,
        description="Return fraction that fires the first (partial) take-profit.",
    )
    post_scale_out_stop_pct: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description=(
            "Return-fraction level the remainder's stop moves to after "
            "scaling out. 0.0 = breakeven. A fixed level, not a trailing "
            "stop — see module docstring."
        ),
    )
    final_take_profit_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description=(
            "Optional second take-profit target for the post-scale-out "
            "remainder. None disables it — the remainder then only exits "
            "via post_scale_out_stop or peak_drawdown."
        ),
    )
    tick_interval_seconds: int = Field(
        default=30,
        ge=5,
        le=3600,
        description="Seconds between exit-evaluation ticks.",
    )


class ScaleOutExitV0:
    SECTION_TYPE = "exit"
    SECTION_VERSION = "0.1.0"
    REQUIRES = ["market_data", "portfolio"]
    Config = ScaleOutExitConfig

    def __init__(self, config: ScaleOutExitConfig) -> None:
        self.config = config

    def run(self, input: SectionInput) -> SectionOutput:
        pos = input.payload
        if not isinstance(pos, MarkedPosition):
            return SectionOutput(payload=None, verdict="skip", reason="no position upstream")
        if pos.avg_entry_price <= 0:
            return SectionOutput(payload=None, verdict="skip", reason="invalid avg_entry_price")

        return_pct = (pos.current_price - pos.avg_entry_price) / pos.avg_entry_price

        cost_basis = pos.avg_entry_price * pos.qty
        peak_gain_usd = (pos.peak_price - pos.avg_entry_price) * pos.qty
        floor = max(
            self.config.peak_meaningful_floor_usd,
            self.config.peak_meaningful_floor_pct * cost_basis,
        )
        peak_meaningful = pos.peak_price > pos.avg_entry_price and peak_gain_usd >= floor
        if peak_meaningful:
            peak_dd = (pos.peak_price - pos.current_price) / (pos.peak_price - pos.avg_entry_price)
        else:
            peak_dd = 0.0

        signals: dict[str, object] = {
            "return_pct": round(return_pct, 4),
            "peak_price": round(pos.peak_price, 4),
            "peak_dd": round(peak_dd, 4) if peak_meaningful else None,
            "peak_meaningful": peak_meaningful,
            "scaled_out": pos.scaled_out,
        }

        trigger: Trigger | None
        close_qty = pos.qty
        if not pos.scaled_out:
            if return_pct <= -self.config.stop_loss_pct:
                trigger = "stop_loss"
            elif peak_meaningful and peak_dd >= self.config.peak_drawdown_pct:
                trigger = "peak_drawdown"
            elif return_pct >= self.config.scale_out_trigger_pct:
                trigger = "scale_out"
                close_qty = pos.qty * self.config.scale_out_fraction
            else:
                trigger = None
        else:
            if return_pct <= self.config.post_scale_out_stop_pct:
                trigger = "post_scale_out_stop"
            elif peak_meaningful and peak_dd >= self.config.peak_drawdown_pct:
                trigger = "peak_drawdown"
            elif (
                self.config.final_take_profit_pct is not None
                and return_pct >= self.config.final_take_profit_pct
            ):
                trigger = "final_take_profit"
            else:
                trigger = None

        if trigger is None:
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason="within thresholds",
                signals=signals,
            )

        intent = CloseIntent(
            market_id=pos.market_id,
            side=pos.side,
            price=pos.current_price,
            qty=close_qty,
            trigger=trigger,
        )
        signals["trigger"] = trigger
        return SectionOutput(
            payload=intent,
            verdict="ok",
            reason=trigger,
            signals=signals,
        )

    @staticmethod
    def CONTRACT_TEST() -> None:
        inst = ScaleOutExitV0(ScaleOutExitConfig())

        out_skip = inst.run(SectionInput(tick_type="hard", payload=None))
        assert out_skip.verdict == "skip"

        hold = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.52,
            peak_price=0.52,
        )
        out_hold = inst.run(SectionInput(tick_type="hard", payload=hold))
        assert out_hold.verdict == "skip"

        win = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.65,
            peak_price=0.65,
        )
        out_scale = inst.run(SectionInput(tick_type="hard", payload=win))
        assert out_scale.verdict == "ok"
        assert isinstance(out_scale.payload, CloseIntent)
        assert out_scale.payload.trigger == "scale_out"
        assert out_scale.payload.qty == 10.0  # 20 * 0.5

        scaled = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=10.0,
            current_price=0.49,
            peak_price=0.65,
            scaled_out=True,
        )
        out_stop = inst.run(SectionInput(tick_type="hard", payload=scaled))
        assert out_stop.verdict == "ok"
        assert out_stop.payload.trigger == "post_scale_out_stop"
        assert out_stop.payload.qty == 10.0
