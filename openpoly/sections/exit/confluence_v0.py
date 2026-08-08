"""Confluence exit — peak-drawdown tolerance keyed on how many headlines agree.

The baseline ``ThresholdExitV0`` applies one ``peak_drawdown_pct`` to every
position regardless of how much evidence stands behind it. That treats a
position backed by a single headline and one backed by four independent
confirmations as the same risk, and it treats a position the pipeline has
since argued *against* as the same risk too.

This section reads the position's news-confluence ledger (see
``openpoly.portfolio.confluence``) and picks its trailing threshold from the
resulting regime::

    solo        only the opening news is live      → solo_peak_drawdown_pct
    reinforced  >= reinforce_min_count same-side   → reinforced_peak_drawdown_pct
    contested   an opposing decision has joined    → contested_peak_drawdown_pct

The trading belief: repeat agreement means the move is real and the position
should be given room to ride out noise, while disagreement means a reversion is
plausible and the leash should shorten.

Three deliberate constraints:

- **``stop_loss_pct`` is NOT state-keyed.** It stays an absolute floor in every
  regime. Confluence widens how much *banked gain* a position may give back; it
  never widens how much capital it may lose.
- **``reinforced_peak_drawdown_pct`` is ``float | None``, and ``None`` means
  off.** ``0.0`` would be the opposite of off: ``peak_dd >= 0.0`` is true the
  moment the peak becomes meaningful, so a zero would fire instantly. Same
  disabled-sentinel convention as ``ScaleOutExitConfig.final_take_profit_pct``.
- **The section derives the regime itself**, from the raw signals the runtime
  injects into ``MarkedPosition``. TTL and count thresholds are config, so
  computing the state upstream would leak this section's config into the
  monitor and break ``run()``'s pure-function contract.

Trigger precedence is ``stop_loss → contested_exit → peak_drawdown →
take_profit``. The absolute-loss circuit stays outermost (a prior project's v6
ordering, kept by both other exit impls). ``contested_exit`` sits next because
a thesis that has been contradicted ``contested_close_after`` times should be
exited whether the position is up or down — it outranks take-profit for the
same reason.

Imports ``MarkedPosition``, ``CloseIntent`` and ``Trigger`` from
``threshold_v0`` rather than redefining them — ``ExitMonitor`` constructs the
former directly and ``isinstance``-checks the latter, so a same-named local
class here would make every close a silent no-op. See that module's docstring.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from openpoly.portfolio.confluence import evaluate
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.exit.threshold_v0 import CloseIntent, MarkedPosition, Trigger, peak_drawdown


class ConfluenceExitConfig(BaseModel):
    take_profit_pct: float = Field(
        default=0.20,
        ge=0.0,
        le=10.0,
        description="Close the position when its return reaches this fraction (0.20 = +20%).",
    )
    stop_loss_pct: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description=(
            "Close the position when its loss reaches this fraction (0.15 = -15%). "
            "Deliberately NOT affected by the confluence state — an absolute floor "
            "in every regime."
        ),
    )
    solo_peak_drawdown_pct: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description=(
            "Peak-drawdown threshold while only the opening news supports the "
            "position (0.30 = close after giving back 30% of the peak gain)."
        ),
    )
    reinforced_peak_drawdown_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Peak-drawdown threshold once reinforce_min_count same-side news "
            "items support the position. Empty = peak-drawdown DISABLED in this "
            "state, letting a well-supported position ride out a retrace. Note "
            "0.0 is not 'off' — it fires immediately; leave the field empty to "
            "disable. stop_loss_pct still applies."
        ),
    )
    contested_peak_drawdown_pct: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description=(
            "Peak-drawdown threshold once a news item arguing the OPPOSITE side "
            "of this market has joined — a reversion is plausible, so bank the "
            "gain sooner."
        ),
    )
    reinforce_min_count: int = Field(
        default=2,
        ge=2,
        le=20,
        description=(
            "How many live same-side news items (the opening one included) put "
            "the position into the reinforced state."
        ),
    )
    support_ttl_minutes: int = Field(
        default=120,
        ge=0,
        le=10_080,
        description=(
            "How long a supporting news item keeps counting. 0 = never expires. "
            "Opposing news never expires either way — a contested thesis stays "
            "contested for the life of the position."
        ),
    )
    min_signal_confidence: Literal["low", "medium", "high"] = Field(
        default="low",
        description=(
            "Ignore attached news whose analyzer confidence is below this. 'low' counts everything."
        ),
    )
    contested_close_after: int = Field(
        default=0,
        ge=0,
        le=20,
        description=(
            "Close the position outright once this many opposing news items have "
            "joined. 0 disables — a contradiction then only tightens the "
            "peak-drawdown threshold."
        ),
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
    tick_interval_seconds: int = Field(
        default=30,
        ge=5,
        le=3600,
        description="Seconds between exit-evaluation ticks.",
    )


class ConfluenceExitV0:
    SECTION_TYPE = "exit"
    SECTION_VERSION = "0.1.0"
    REQUIRES = ["market_data", "portfolio"]
    Config = ConfluenceExitConfig

    def __init__(self, config: ConfluenceExitConfig) -> None:
        self.config = config

    def _peak_drawdown_pct(self, state: str) -> float | None:
        """The threshold this regime applies, or None when peak-drawdown is
        disabled for it."""
        if state == "contested":
            return self.config.contested_peak_drawdown_pct
        if state == "reinforced":
            return self.config.reinforced_peak_drawdown_pct
        return self.config.solo_peak_drawdown_pct

    def run(self, input: SectionInput) -> SectionOutput:
        pos = input.payload
        if not isinstance(pos, MarkedPosition):
            return SectionOutput(payload=None, verdict="skip", reason="no position upstream")
        if pos.avg_entry_price <= 0:
            return SectionOutput(payload=None, verdict="skip", reason="invalid avg_entry_price")

        conf = evaluate(
            pos.news_signals,
            position_side=pos.side,
            now=pos.marked_at,
            support_ttl_seconds=self.config.support_ttl_minutes * 60.0,
            reinforce_min_count=self.config.reinforce_min_count,
            min_confidence=self.config.min_signal_confidence,
        )

        return_pct = (pos.current_price - pos.avg_entry_price) / pos.avg_entry_price

        peak_meaningful, peak_dd = peak_drawdown(
            pos,
            floor_usd=self.config.peak_meaningful_floor_usd,
            floor_pct=self.config.peak_meaningful_floor_pct,
        )

        dd_limit = self._peak_drawdown_pct(conf.state)

        signals: dict[str, object] = {
            "return_pct": round(return_pct, 4),
            "peak_price": round(pos.peak_price, 4),
            "peak_dd": round(peak_dd, 4) if peak_meaningful else None,
            "peak_meaningful": peak_meaningful,
            "confluence_state": conf.state,
            "support": conf.support,
            "against": conf.against,
            "peak_drawdown_pct": dd_limit,
        }

        trigger: Trigger | None
        if return_pct <= -self.config.stop_loss_pct:
            trigger = "stop_loss"
        elif (
            self.config.contested_close_after > 0
            and conf.against >= self.config.contested_close_after
        ):
            trigger = "contested_exit"
        elif peak_meaningful and dd_limit is not None and peak_dd >= dd_limit:
            trigger = "peak_drawdown"
        elif return_pct >= self.config.take_profit_pct:
            trigger = "take_profit"
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
            qty=pos.qty,
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
        from openpoly.portfolio.models import PositionSignal

        inst = ConfluenceExitV0(ConfluenceExitConfig())

        out_skip = inst.run(SectionInput(tick_type="hard", payload=None))
        assert out_skip.verdict == "skip"

        def sig(sid: int, side: str, ts: float) -> PositionSignal:
            return PositionSignal(
                id=sid,
                position_id=1,
                news_id=f"n{sid}",
                ts=ts,
                side=side,  # type: ignore[arg-type]
                relation="opening" if sid == 1 else "reinforce",
                p_model=0.7,
                confidence="high",
            )

        # Ran up to 0.62 (+24%, peak gain $2.40 >= floor), now 0.58 → retrace
        # 4/12 = 33%. Under the solo threshold (0.30) that closes; under
        # reinforced (disabled by default) it holds. Same mark, both ways.
        def retrace(signals: tuple[PositionSignal, ...]) -> MarkedPosition:
            return MarkedPosition(
                market_id="m1",
                side="yes",
                avg_entry_price=0.50,
                qty=20.0,
                current_price=0.58,
                peak_price=0.62,
                news_signals=signals,
                marked_at=1000.0,
            )

        out_solo = inst.run(
            SectionInput(tick_type="hard", payload=retrace((sig(1, "yes", 990.0),)))
        )
        assert out_solo.verdict == "ok"
        assert isinstance(out_solo.payload, CloseIntent)
        assert out_solo.payload.trigger == "peak_drawdown"
        assert out_solo.signals["confluence_state"] == "solo"

        two_yes = (sig(1, "yes", 990.0), sig(2, "yes", 995.0))
        out_reinforced = inst.run(SectionInput(tick_type="hard", payload=retrace(two_yes)))
        assert out_reinforced.verdict == "skip"
        assert out_reinforced.signals["confluence_state"] == "reinforced"

        contested = (*two_yes, sig(3, "no", 998.0))
        out_contested = inst.run(SectionInput(tick_type="hard", payload=retrace(contested)))
        assert out_contested.verdict == "ok"
        assert out_contested.payload.trigger == "peak_drawdown"
        assert out_contested.signals["confluence_state"] == "contested"

        # stop_loss ignores the confluence state entirely.
        loss = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.40,
            peak_price=0.52,
            news_signals=two_yes,
            marked_at=1000.0,
        )
        out_sl = inst.run(SectionInput(tick_type="hard", payload=loss))
        assert out_sl.verdict == "ok"
        assert out_sl.payload.trigger == "stop_loss"
