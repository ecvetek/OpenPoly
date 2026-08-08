"""Execution — the level-1 paper fill service.

A fixed system service (not a pluggable section): it turns an entry
``OrderIntent`` or an exit close decision into an actual fill, recorded through
``PortfolioStore``. The fill model is deliberately crude — it takes the order
book's level-1 price (BUY at the best ask, SELL at the best bid) and caps a buy
by that level's depth. No walk-book, no fees (zero-fee rule). At micro-stakes
($5-$50) an order rarely walks past level 1, so this is not worth more. The
one exception is ``OrderIntent.slippage_tolerance`` — a buy is rejected
outright (not filled at a worse price) if the current ask has moved beyond
that tolerance since the entry section's decision; see ``execute_buy``.

Entry and exit share this one executor so their accounting is symmetric. It
reads the live ``MarketStore`` singleton directly (same pattern as the
embedding section — no capability injection). Construction touches no DB; the
``PortfolioStore`` is injected by the FastAPI lifespan once the database is up.
"""

from __future__ import annotations

import logging

from openpoly.execution.signal_log import attach_signal
from openpoly.execution.types import ExecResult
from openpoly.markets.manager import manager as market_source_manager
from openpoly.portfolio import CloseReason, HeldPosition, PortfolioStore, Side
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

logger = logging.getLogger(__name__)

# A BUY fill below this notional (USD) is not worth recording — mirrors the
# real Polymarket CLOB's own $1 marketable-BUY minimum (see
# LiveExecutor's _MIN_NOTIONAL_PUSD). SELL has no equivalent: the exchange
# enforces no minimum notional on the taker side, only a decimal-precision
# rule (see execute_sell below) — so this constant is BUY-only.
MIN_FILL_USD = 1.0


class PaperExecutor:
    """Level-1 paper fill service. Routed to by ExecutorDispatcher when
    ``runtime_state.exec_mode == "paper"`` (default)."""

    def __init__(self, portfolio: PortfolioStore | None = None) -> None:
        self._portfolio = portfolio

    def configure(self, portfolio: PortfolioStore) -> None:
        """Inject the PortfolioStore. The FastAPI lifespan calls this once the
        database is up; tests pass a store to ``__init__`` directly."""
        self._portfolio = portfolio

    @property
    def _store(self) -> PortfolioStore:
        if self._portfolio is None:
            raise RuntimeError("Executor has no PortfolioStore — call configure() first")
        return self._portfolio

    @property
    def portfolio(self) -> PortfolioStore | None:
        """The injected store, or None before ``configure()``. The non-raising
        sibling of ``_store``: the entry section's ``portfolio_provider`` is
        called on every run() and legitimately may fire before the lifespan has
        configured one."""
        return self._portfolio

    def execute_buy(
        self,
        intent: OrderIntent,
        *,
        news_id: str | None,
        ts: float,
        p_model: float | None = None,
        confidence: str | None = None,
    ) -> ExecResult:
        """Open a position from an entry ``OrderIntent`` at the level-1 ask.

        Skips (nothing opened) when a position on this market is already open —
        on either side — when the market / order book / ask liquidity is
        missing, when the current ask has moved beyond
        ``intent.slippage_tolerance`` of ``intent.price`` (``slippage_exceeded``
        — never fills at a worse price than the entry section tolerated), or
        when the fill notional rounds to dust.

        A skip on an existing position is not a dead end: the decision is
        attached to that position as a news-confluence signal (``reinforce``
        for the same side, ``contradict`` for the opposite), which is what the
        confluence exit section reads to pick its drawdown threshold.
        ``p_model`` / ``confidence`` come from the analyzer and are snapshotted
        onto that signal.

        The two position checks run BEFORE the market / book lookups so a
        repeat headline still attaches when the book happens to be dark — the
        news is information regardless of whether we could have traded on it.
        (This also matches ``LiveExecutor``'s ordering.)
        """
        existing = self._store.get_open_position(intent.market_id, intent.side)
        if existing is not None:
            attach_signal(
                self._store,
                existing.position_id,
                news_id=news_id,
                ts=ts,
                side=intent.side,
                relation="reinforce",
                p_model=p_model,
                confidence=confidence,
            )
            return ExecResult.skip("position_exists", position_id=existing.position_id)

        # YES + NO on one market settle to exactly $1, so holding both is a
        # locked loss of the two spreads with no upside — never a hedge. Block
        # it and record the decision as a contradiction instead.
        opposite: Side = "no" if intent.side == "yes" else "yes"
        blocking = self._store.get_open_position(intent.market_id, opposite)
        if blocking is not None:
            attach_signal(
                self._store,
                blocking.position_id,
                news_id=news_id,
                ts=ts,
                side=intent.side,
                relation="contradict",
                p_model=p_model,
                confidence=confidence,
            )
            return ExecResult.skip("opposite_position_exists", position_id=blocking.position_id)

        catalog = market_source_manager.store
        market = catalog.get(intent.market_id)
        if market is None:
            return ExecResult.skip("market_not_found")

        token_id = market.yes_token_id if intent.side == "yes" else market.no_token_id
        if token_id is None:
            return ExecResult.skip("no_token")

        book = catalog.get_order_book(token_id)
        if book is None:
            return ExecResult.skip("no_order_book")
        if not book.asks:
            return ExecResult.skip("no_ask_liquidity")

        ask_price, ask_size = book.asks[0]
        if ask_price > intent.price * (1 + intent.slippage_tolerance):
            return ExecResult.skip("slippage_exceeded")
        qty = min(intent.qty, ask_size)
        if qty * ask_price < MIN_FILL_USD:
            return ExecResult.skip("dust")

        held = self._store.open_position(
            market_id=intent.market_id,
            side=intent.side,
            token_id=token_id,
            condition_id=market.condition_id,
            price=ask_price,
            qty=qty,
            ts=ts,
            news_id=news_id,
        )
        attach_signal(
            self._store,
            held.position_id,
            news_id=news_id,
            ts=ts,
            side=intent.side,
            relation="opening",
            p_model=p_model,
            confidence=confidence,
        )
        logger.info(
            "buy filled: %s %s qty=%.4f @ %.4f (position %d)",
            intent.market_id,
            intent.side,
            qty,
            ask_price,
            held.position_id,
        )
        return ExecResult.ok(price=ask_price, qty=qty, position_id=held.position_id)

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: CloseReason,
        ts: float,
        trigger: str | None = None,
        qty: float | None = None,
    ) -> ExecResult:
        """Close a held position at the level-1 bid of its own token's book.

        ``qty`` requests a partial sell (e.g. a scale-out) — defaults to the
        full remaining ``position.qty`` for every existing caller (settlement,
        manual close, the baseline exit section). Still capped by
        ``position.qty`` so a caller can never request more than is actually
        held.

        Reads ``position.token_id`` directly, so a close never depends on the
        market still being in the live catalog. Skips when the order book /
        bid liquidity is missing.

        Capped by level-1 bid depth, symmetric with ``execute_buy``'s ask-depth
        cap. This previously sold the whole position at the best bid regardless
        of how thin the book was, so paper never partially filled an exit while
        live routinely does — paper systematically overstated exit liquidity and
        its results weren't comparable to a live run. A partial goes through
        ``record_sell``, which keeps the position open with the remainder for
        the next exit tick, exactly as the live path does.

        No dollar-notional floor, unlike ``execute_buy`` — the real Polymarket
        CLOB enforces a $1 minimum on marketable BUYs but not on SELLs (see
        ``LiveExecutor.execute_sell``'s own module comment, "verified by live
        smoke"), so a paper/backtest floor here would refuse fills live mode
        would actually execute (a thin scale-out remainder or stop-loss dust),
        making simulated results diverge from what live would really do. Still
        skipped as "dust" if the fill would be zero or negative (e.g. a caller
        requesting ``qty=0``) — not a real fill either way.
        """
        book = market_source_manager.store.get_order_book(position.token_id)
        if book is None:
            return ExecResult.skip("no_order_book")
        if not book.bids:
            return ExecResult.skip("no_bid_liquidity")

        bid_price, bid_size = book.bids[0]
        requested = min(qty, position.qty) if qty is not None else position.qty
        fill_qty = min(requested, bid_size)
        if fill_qty <= 0:
            return ExecResult.skip("dust")

        self._store.record_sell(
            position.position_id,
            sold_qty=fill_qty,
            sell_price=bid_price,
            ts=ts,
            close_reason=close_reason,
            trigger=trigger,
        )
        logger.info(
            "sell filled: %s %s qty=%.4f/%.4f @ %.4f (position %d, %s)",
            position.market_id,
            position.side,
            fill_qty,
            position.qty,
            bid_price,
            position.position_id,
            close_reason,
        )
        return ExecResult.ok(
            price=bid_price,
            qty=fill_qty,
            position_id=position.position_id,
        )
