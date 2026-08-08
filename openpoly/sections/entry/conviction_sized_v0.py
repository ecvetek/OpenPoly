"""Conviction-sized entry — edge-threshold entry with confidence-tiered sizing.

A self-contained fork of ``edge_threshold_v0.EdgeThresholdEntryV0``: identical
gating (edge/spread, late-buy veto, cooldown/lockout, heat cap, A4 kill
switches — see that module's docstring for the full rationale on each,
including the news-confluence bypass: all of those gates defer to an
already-open position on this market, either side), with exactly one
change — position size scales with the analyzer's ``confidence`` grade
instead of a flat ``order_size_usd``::

    qty = (order_size_usd * multiplier_for(confidence)) / held_price

Duplicated rather than extracted into a shared helper, deliberately:

- Zero risk to ``edge_threshold_v0.py``, the file running live capital.
- The registry requires the concrete class to live in its own leaf module
  regardless (``_registry._extract_section_classes`` checks
  ``obj.__module__ != module_name``), so extraction would only save line
  count, not file count, at the cost of coupling two independently-tuned
  strategies.
- Matches the project's own bias (CHANGELOG 05/24, rejecting a trailing-stop
  variant) against adding structural complexity a two-variant case doesn't
  yet justify.

Imports ``OrderIntent`` from ``edge_threshold_v0`` rather than redefining it —
the orchestrator's ``isinstance(out.payload, OrderIntent)`` check
(``openpoly/runtime/orchestrator.py``) is keyed on that exact class, so a
same-named-but-distinct local dataclass would make every entry here a silent
no-op: verdict "ok", but the executor never called.
"""

from __future__ import annotations

import time
from typing import Callable, Literal, TYPE_CHECKING

from pydantic import BaseModel, Field

from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.polymarket_api import recent_move
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

if TYPE_CHECKING:
    from openpoly.portfolio import PortfolioStore


Side = Literal["yes", "no"]
PortfolioProvider = Callable[[], "PortfolioStore | None"]


class ConvictionSizedConfig(BaseModel):
    min_edge: float = Field(default=0.05, ge=0.0, le=1.0)
    order_size_usd: float = Field(
        default=10.0,
        ge=1.0,
        le=100.0,
        description="Base order size in USD, before the confidence multiplier.",
    )
    max_spread: float = Field(default=0.05, ge=0.0, le=0.5)
    slippage_tolerance: float = Field(
        default=0.02,
        ge=0.0,
        le=0.2,
        description=(
            "How far the book may move against this decision before fill "
            "time (fraction of price). Paper rejects the fill if the "
            "current ask exceeds price * (1 + this); live widens its limit "
            "order by the same amount so it can still cross a moved book."
        ),
    )
    side_lock: bool = Field(
        default=False,
        description=(
            "Lock to YES only; never buy NO. Bypassed when this market "
            "already has an open position (either side) — see "
            "edge_threshold_v0.py's module docstring."
        ),
    )
    low_multiplier: float = Field(
        default=0.5,
        ge=0.1,
        le=3.0,
        description="order_size_usd multiplier when the analyzer's confidence is low.",
    )
    medium_multiplier: float = Field(
        default=1.0,
        ge=0.1,
        le=3.0,
        description="order_size_usd multiplier when the analyzer's confidence is medium.",
    )
    high_multiplier: float = Field(
        default=1.5,
        ge=0.1,
        le=3.0,
        description="order_size_usd multiplier when the analyzer's confidence is high.",
    )
    veto_enabled: bool = Field(
        default=False,
        description=(
            "Enable the late-buy veto. Off by default — run warn-only first "
            "and observe recent_move before enforcing."
        ),
    )
    veto_window_min: int = Field(
        default=60,
        ge=1,
        le=1440,
        description="Late-buy veto: price-move lookback window, in minutes.",
    )
    veto_move_threshold: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description=(
            "Late-buy veto: skip the entry if the held side's token has "
            "already moved up by at least this much over the window."
        ),
    )
    same_market_cooldown_minutes: int = Field(
        default=0,
        ge=0,
        le=1440,
        description=(
            "Skip the entry if a position on the same (market, side) was "
            "opened or closed within this many minutes. 0 disables the "
            "check. Superseded by ``same_market_lifetime_lockout`` when "
            "that is True. Bypassed when this market already has an open "
            "position on either side."
        ),
    )
    same_market_lifetime_lockout: bool = Field(
        default=False,
        description=(
            "Strict mode: skip if ANY prior position exists on (market, "
            "side), regardless of when. When True, "
            "``same_market_cooldown_minutes`` is ignored. Bypassed when "
            "this market already has an open position on either side."
        ),
    )
    heat_cap_usd: float = Field(
        default=0.0,
        ge=0.0,
        le=10_000.0,
        description=(
            "Skip the entry if the sum of (qty × avg_entry_price) across "
            "all currently-open positions is at or above this dollar "
            "amount. 0 disables the check. Bypassed when this market "
            "already has an open position on either side."
        ),
    )

    # ---- A4 kill switch (entry-side circuit breakers) ----
    # Also bypassed, same as heat_cap, when this market already has an open
    # position on either side.
    kill_max_consecutive_losses: int = Field(
        default=0,
        ge=0,
        le=100,
        description=(
            "Skip the entry if the most recent N closed positions are ALL "
            "losses (realized_pnl < 0). 0 disables."
        ),
    )
    kill_daily_loss_usd: float = Field(
        default=0.0,
        ge=0.0,
        le=10_000.0,
        description=(
            "Skip the entry if the sum of realized_pnl across positions "
            "closed in the last 24h is ≤ -kill_daily_loss_usd. 0 disables."
        ),
    )
    kill_max_drawdown_usd: float = Field(
        default=0.0,
        ge=0.0,
        le=10_000.0,
        description=(
            "Skip the entry if the cumulative realized_pnl curve (all "
            "closed positions, chronological) has dropped this many "
            "dollars from its peak. 0 disables."
        ),
    )


class ConvictionSizedEntryV0:
    SECTION_TYPE = "entry"
    SECTION_VERSION = "0.1.0"
    REQUIRES = ["order_book", "market_data"]
    Config = ConvictionSizedConfig

    def __init__(
        self,
        config: ConvictionSizedConfig,
        portfolio_provider: PortfolioProvider | None = None,
    ) -> None:
        self.config = config
        self._portfolio_provider = portfolio_provider

    def run(self, input: SectionInput) -> SectionOutput:
        res = input.payload
        if not isinstance(res, AnalysisResult):
            return SectionOutput(payload=None, verdict="skip", reason="no analysis upstream")

        side: Side = "yes" if res.p_model >= 0.5 else "no"

        # Portfolio-aware gates — cheapest first. Only fetch the portfolio
        # when at least one gate is enabled (keeps the default config from
        # touching the DB at all, which contract tests rely on). side_lock is
        # included here now too, even though it needs no portfolio data of
        # its own — it needs the confluence bypass below, which does.
        needs_portfolio = (
            self.config.side_lock
            or self.config.heat_cap_usd > 0
            or self.config.same_market_lifetime_lockout
            or self.config.same_market_cooldown_minutes > 0
            or self.config.kill_max_consecutive_losses > 0
            or self.config.kill_daily_loss_usd > 0
            or self.config.kill_max_drawdown_usd > 0
        )
        portfolio = (
            self._portfolio_provider() if needs_portfolio and self._portfolio_provider else None
        )

        # News-confluence bypass — see edge_threshold_v0.py's module
        # docstring and its own copy of this comment for the full rationale.
        # An open position on this market (either side) means no NEW capital
        # is at stake, so side_lock / heat_cap / the kill switches /
        # cooldown-lockout all defer to the executor's own
        # position_exists / opposite_position_exists check instead of
        # blocking the decision from ever reaching it.
        has_existing = False
        if portfolio is not None:
            opposite: Side = "no" if side == "yes" else "yes"
            has_existing = (
                portfolio.get_open_position(res.market_id, side) is not None
                or portfolio.get_open_position(res.market_id, opposite) is not None
            )

        if not has_existing:
            if self.config.side_lock and side != "yes":
                return SectionOutput(payload=None, verdict="skip", reason="side_lock active")

            if portfolio is not None:
                cap_usd = self.config.heat_cap_usd
                if cap_usd > 0:
                    opens = portfolio.get_open_positions()
                    open_cost = sum(h.qty * h.avg_entry_price for h in opens)
                    if open_cost >= cap_usd:
                        return SectionOutput(
                            payload=None,
                            verdict="skip",
                            reason="heat_cap",
                            signals={
                                "side": side,
                                "open_cost": round(open_cost, 2),
                                "heat_cap_usd": cap_usd,
                                "open_position_count": len(opens),
                            },
                        )

                kill_skip = _kill_switch_check(portfolio, self.config, now=None)
                if kill_skip is not None:
                    reason, signals = kill_skip
                    return SectionOutput(
                        payload=None,
                        verdict="skip",
                        reason=reason,
                        signals={"side": side, **signals},
                    )

                if self.config.same_market_lifetime_lockout:
                    if _market_side_has_history(portfolio, res.market_id, side):
                        return SectionOutput(
                            payload=None,
                            verdict="skip",
                            reason="same_market_lockout",
                            signals={"side": side},
                        )
                elif self.config.same_market_cooldown_minutes > 0:
                    cooldown_min = self.config.same_market_cooldown_minutes
                    if _in_cooldown(portfolio, res.market_id, side, cooldown_min):
                        return SectionOutput(
                            payload=None,
                            verdict="skip",
                            reason="same_market_cooldown",
                            signals={
                                "side": side,
                                "cooldown_minutes": cooldown_min,
                            },
                        )

        catalog = market_source_manager.store
        market = catalog.get(res.market_id)
        if market is None:
            return SectionOutput(payload=None, verdict="skip", reason="market not found")
        if not market.tradeable:
            return SectionOutput(
                payload=None, verdict="skip", reason="not_tradeable", signals={"side": side}
            )
        token_id = market.yes_token_id if side == "yes" else market.no_token_id
        if token_id is None:
            return SectionOutput(payload=None, verdict="skip", reason="no token for side")

        book = catalog.get_order_book(token_id)
        if book is None or not book.asks or not book.bids:
            return SectionOutput(payload=None, verdict="skip", reason="no order book")
        held_price = book.asks[0][0]
        if held_price <= 0.0:
            return SectionOutput(payload=None, verdict="skip", reason="invalid ask price")

        spread = held_price - book.bids[0][0]
        fair = res.p_model if side == "yes" else 1.0 - res.p_model
        edge = fair - held_price
        signals = {
            "side": side,
            "edge": round(edge, 4),
            "spread": round(spread, 4),
            "p_model": res.p_model,
            "held_price": held_price,
            "min_edge": self.config.min_edge,
            "max_spread": self.config.max_spread,
        }

        if edge < self.config.min_edge:
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason="edge below min_edge",
                signals=signals,
            )
        if spread > self.config.max_spread:
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason="spread above max_spread",
                signals=signals,
            )

        if self.config.veto_enabled:
            move = recent_move(token_id, window_min=self.config.veto_window_min)
            if move is not None:
                signals["recent_move"] = round(move, 4)
                if move >= self.config.veto_move_threshold:
                    return SectionOutput(
                        payload=None,
                        verdict="skip",
                        reason="late buy",
                        signals=signals,
                    )

        multiplier = _multiplier_for(self.config, res.confidence)
        signals["confidence"] = res.confidence
        signals["size_multiplier"] = multiplier
        qty = (self.config.order_size_usd * multiplier) / held_price
        intent = OrderIntent(
            market_id=res.market_id,
            side=side,
            price=held_price,
            qty=qty,
            slippage_tolerance=self.config.slippage_tolerance,
        )
        return SectionOutput(payload=intent, verdict="ok", signals=signals)

    @staticmethod
    def CONTRACT_TEST() -> None:
        inst = ConvictionSizedEntryV0(ConvictionSizedConfig())

        out_none = inst.run(SectionInput(tick_type="event", payload=None))
        assert out_none.verdict == "skip"

        res = AnalysisResult(market_id="__contract_test__", p_model=0.6, confidence="medium")
        out_no_market = inst.run(SectionInput(tick_type="event", payload=res))
        assert out_no_market.verdict == "skip"


def _multiplier_for(config: ConvictionSizedConfig, confidence: str) -> float:
    return {
        "low": config.low_multiplier,
        "medium": config.medium_multiplier,
        "high": config.high_multiplier,
    }[confidence]


def _in_cooldown(
    portfolio: "PortfolioStore",
    market_id: str,
    side: Side,
    cooldown_minutes: int,
    now: float | None = None,
) -> bool:
    """True iff the most recent position on (market_id, side) was opened OR
    closed within ``cooldown_minutes``. See edge_threshold_v0._in_cooldown for
    the full scan-every-match rationale (duplicated here, not shared)."""
    cutoff_ts = (now if now is not None else time.time()) - cooldown_minutes * 60
    for pos in portfolio.list_positions(limit=500):
        if pos.market_id != market_id or pos.side != side:
            continue
        ref_ts = pos.closed_at if pos.closed_at is not None else pos.opened_at
        if ref_ts > cutoff_ts:
            return True
    return False


def _market_side_has_history(
    portfolio: "PortfolioStore",
    market_id: str,
    side: Side,
) -> bool:
    """True iff any prior position on (market_id, side) exists — open or
    closed, no time window. Backs ``same_market_lifetime_lockout``."""
    for pos in portfolio.list_positions(limit=500):
        if pos.market_id == market_id and pos.side == side:
            return True
    return False


def _kill_switch_check(
    portfolio: "PortfolioStore",
    config: "ConvictionSizedConfig",
    *,
    now: float | None = None,
) -> tuple[str, dict] | None:
    """A4 portfolio-wide circuit breakers — see edge_threshold_v0's version
    for the full precedence rationale (consecutive → daily → drawdown)."""
    positions = portfolio.list_positions(limit=500)
    closed = [p for p in positions if p.closed_at is not None and p.realized_pnl is not None]
    if not closed:
        return None
    closed.sort(key=lambda p: p.closed_at, reverse=True)  # newest close first
    now_ts = now if now is not None else time.time()

    if config.kill_max_consecutive_losses > 0:
        streak = 0
        for p in closed:
            if p.realized_pnl < 0:
                streak += 1
            else:
                break
        if streak >= config.kill_max_consecutive_losses:
            return (
                "kill_consecutive_losses",
                {"streak": streak, "limit": config.kill_max_consecutive_losses},
            )

    if config.kill_daily_loss_usd > 0:
        cutoff = now_ts - 86400.0
        daily_pnl = sum(p.realized_pnl for p in closed if p.closed_at >= cutoff)
        if daily_pnl <= -config.kill_daily_loss_usd:
            return (
                "kill_daily_loss",
                {
                    "daily_pnl_usd": round(daily_pnl, 2),
                    "limit_usd": config.kill_daily_loss_usd,
                },
            )

    if config.kill_max_drawdown_usd > 0:
        cum = 0.0
        peak = 0.0
        for p in reversed(closed):
            cum += p.realized_pnl
            if cum > peak:
                peak = cum
        drawdown = peak - cum
        if drawdown >= config.kill_max_drawdown_usd:
            return (
                "kill_drawdown",
                {
                    "drawdown_usd": round(drawdown, 2),
                    "peak_usd": round(peak, 2),
                    "limit_usd": config.kill_max_drawdown_usd,
                },
            )

    return None
