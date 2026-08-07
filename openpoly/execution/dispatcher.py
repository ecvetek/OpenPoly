"""ExecutorDispatcher — route execute_buy/sell to paper or live by mode.

Implements slice A+B's D2 commitment: "push mode awareness down into a single
spot in the Executor... pulling that out in slice C is exactly this
dispatcher". The orchestrator and exit_monitor
keep their existing contract (call ``executor.execute_*``, get ``ExecResult``);
they have no idea which implementation actually fills.

The live executor is optional — it's configured by the FastAPI lifespan once
the wallet + ClobClient are ready. If mode=live and live is None, we return
``ExecResult.skip("live_not_ready")`` so paper-only deployments still work.
"""

from __future__ import annotations

import logging
from typing import Protocol

from openpoly.execution.types import ExecResult
from openpoly.portfolio import HeldPosition, PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent
from openpoly.wallet.runtime_state import runtime_state

logger = logging.getLogger(__name__)


class _PaperLike(Protocol):
    def configure(self, portfolio: PortfolioStore) -> None: ...
    @property
    def portfolio(self) -> PortfolioStore | None: ...
    def execute_buy(
        self,
        intent: OrderIntent,
        *,
        news_id: str | None,
        ts: float,
        p_model: float | None = None,
        confidence: str | None = None,
    ) -> ExecResult: ...
    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason,
        ts: float,
        trigger: str | None = None,
        qty: float | None = None,
    ) -> ExecResult: ...


class _LiveLike(Protocol):
    def execute_buy(
        self,
        intent: OrderIntent,
        *,
        news_id: str | None,
        ts: float,
        p_model: float | None = None,
        confidence: str | None = None,
    ) -> ExecResult: ...
    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason,
        ts: float,
        trigger: str | None = None,
        qty: float | None = None,
    ) -> ExecResult: ...
    def get_collateral_balance_raw(self) -> int | None: ...


class ExecutorDispatcher:
    """Routes execute_* to paper or live based on runtime_state.exec_mode."""

    def __init__(
        self,
        paper: _PaperLike,
        live: _LiveLike | None = None,
    ) -> None:
        self._paper = paper
        self._live = live

    def configure_paper(self, portfolio: PortfolioStore) -> None:
        """Proxy to PaperExecutor.configure — called by lifespan once DB is up.
        Always called, regardless of current mode."""
        self._paper.configure(portfolio)

    def configure_live(self, live: _LiveLike) -> None:
        """Inject the live executor — called by ``arm_live_executor`` (lifespan
        at startup, and the wallet routes when a wallet is configured or a mode
        flip is requested afterwards). Idempotent; safe to call even when
        mode=paper (live is pre-built so a UI-driven flip is cheap)."""
        self._live = live

    @property
    def portfolio(self) -> PortfolioStore | None:
        """The one PortfolioStore both executors write through, or None before
        the lifespan configures it. Paper and live share it by construction, so
        reading it off the paper side is not a paper-mode detail.

        Exists so the entry section's ``portfolio_provider`` has something
        public to call: it used to be spelled
        ``getattr(getattr(executor, "_paper", executor), "_portfolio", None)``
        — duplicated in two places, reaching through two private attributes,
        and silently degrading to None if either name ever changed."""
        return self._paper.portfolio

    @property
    def live_ready(self) -> bool:
        """True once a live executor has been injected. Read by the health
        check so ``exec_mode == "live"`` with no live executor surfaces as a
        failure instead of a healthy-looking system that skips every order."""
        return self._live is not None

    def get_collateral_balance_raw(self) -> int | None:
        """Read-only USDC collateral balance via the live executor's CLOB
        client. None when no wallet/live executor is configured or the read
        fails. Deliberately mode-independent — the wallet is an on-chain fact,
        so the dashboard shows the same number in paper and live mode."""
        if self._live is None:
            return None
        return self._live.get_collateral_balance_raw()

    def execute_buy(
        self,
        intent: OrderIntent,
        *,
        news_id: str | None,
        ts: float,
        p_model: float | None = None,
        confidence: str | None = None,
    ) -> ExecResult:
        """``p_model`` / ``confidence`` are the analyzer's numbers for this
        decision, passed straight through so the executor can snapshot them
        onto the news-confluence signal it records."""
        if runtime_state.exec_mode == "live":
            if self._live is None:
                logger.warning("dispatch buy: live mode but live executor unconfigured")
                return ExecResult.skip("live_not_ready")
            return self._live.execute_buy(
                intent, news_id=news_id, ts=ts, p_model=p_model, confidence=confidence
            )
        return self._paper.execute_buy(
            intent, news_id=news_id, ts=ts, p_model=p_model, confidence=confidence
        )

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason,
        ts: float,
        trigger: str | None = None,
        qty: float | None = None,
    ) -> ExecResult:
        if runtime_state.exec_mode == "live":
            if self._live is None:
                logger.warning("dispatch sell: live mode but live executor unconfigured")
                return ExecResult.skip("live_not_ready")
            return self._live.execute_sell(
                position, close_reason=close_reason, ts=ts, trigger=trigger, qty=qty
            )
        return self._paper.execute_sell(
            position, close_reason=close_reason, ts=ts, trigger=trigger, qty=qty
        )
