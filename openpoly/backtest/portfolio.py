"""``BacktestPortfolio`` — an in-memory, never-persisted position ledger.

Implements exactly the surface ``PaperExecutor`` and the entry sections'
portfolio-aware gates (cooldown / lockout / heat-cap / kill-switches) call:
``open_position``, ``record_sell``, ``get_open_position``,
``get_open_positions``, ``list_positions``. Both run *completely unmodified*
against this — the whole point being that a backtest reuses the real gating
logic and the real ``PaperExecutor`` fill model, not a second hand-rolled
approximation of either.

Mirrors ``PortfolioStore.record_sell``'s exact close-or-reduce semantics
(same epsilon, same realized-PnL accrual) — duplicated rather than imported,
matching this codebase's existing convention for keeping a read-side/
simulation module independent of the live store's internals (see
``portfolio/statistics.py``'s own duplicated ``_to_record``).

Synthetic position ids start at -1 and count down, so they can never collide
with a real ``PositionRow.id`` (SQLite autoincrement starts at 1) even if a
caller ever mixed the two up by mistake — and a backtest never writes to the
real database at all, so that mistake has no real-data blast radius regardless.
"""

from __future__ import annotations

from dataclasses import dataclass

from openpoly.portfolio.models import CloseReason, HeldPosition, PositionRecord, Side

_QTY_EPS = 1e-6  # mirrors portfolio.store._QTY_EPS


@dataclass
class _MutablePosition:
    id: int
    market_id: str
    side: Side
    token_id: str
    condition_id: str
    qty: float
    avg_entry_price: float
    status: str
    opened_at: float
    closed_at: float | None = None
    close_reason: str | None = None
    realized_pnl: float | None = None


class BacktestPortfolio:
    def __init__(self) -> None:
        self._positions: dict[int, _MutablePosition] = {}
        self._next_id = -1

    def open_position(
        self,
        *,
        market_id: str,
        side: Side,
        token_id: str,
        condition_id: str,
        price: float,
        qty: float,
        ts: float,
        news_id: str | None = None,
        order_id: str | None = None,
        tx_hash: str | None = None,
    ) -> HeldPosition:
        pos_id = self._next_id
        self._next_id -= 1
        pos = _MutablePosition(
            id=pos_id,
            market_id=market_id,
            side=side,
            token_id=token_id,
            condition_id=condition_id,
            qty=qty,
            avg_entry_price=price,
            status="open",
            opened_at=ts,
        )
        self._positions[pos_id] = pos
        return _to_held(pos)

    def record_sell(
        self,
        position_id: int,
        *,
        sold_qty: float,
        sell_price: float,
        ts: float,
        close_reason: CloseReason,
        trigger: str | None = None,
        order_id: str | None = None,
        tx_hash: str | None = None,
    ) -> PositionRecord:
        pos = self._positions[position_id]
        if pos.status != "open":
            raise ValueError(f"position {position_id} is {pos.status}, not open")
        sold = min(sold_qty, pos.qty)
        pos.realized_pnl = (pos.realized_pnl or 0.0) + (sell_price - pos.avg_entry_price) * sold
        if pos.qty - sold <= _QTY_EPS:
            pos.status = "closed"
            pos.closed_at = ts
            pos.close_reason = close_reason
        else:
            pos.qty -= sold
        return _to_record(pos)

    def get_open_position(self, market_id: str, side: Side) -> HeldPosition | None:
        for pos in self._positions.values():
            if pos.status == "open" and pos.market_id == market_id and pos.side == side:
                return _to_held(pos)
        return None

    def get_open_positions(self) -> list[HeldPosition]:
        return [_to_held(p) for p in self._positions.values() if p.status == "open"]

    def list_positions(self, limit: int = 100) -> list[PositionRecord]:
        # Newest-open-first, mirroring PortfolioStore.list_positions' "order
        # by id desc" (ids are assigned in open order here too). The entry
        # gates that consume this either scan every match regardless of
        # order (_in_cooldown, _market_side_has_history) or re-sort
        # explicitly by closed_at (_kill_switch_check), so this ordering
        # choice doesn't affect their correctness — only matches the
        # convention for anything that inspects raw order.
        ordered = sorted(self._positions.values(), key=lambda p: p.id, reverse=True)
        return [_to_record(p) for p in ordered[:limit]]

    def all_closed_ascending(self) -> list[PositionRecord]:
        """Every closed position, ascending by ``closed_at`` — the shape
        ``aggregate_closed_positions`` needs. Not part of the
        ``PortfolioStore`` surface; only the backtest engine calls this,
        once replay finishes."""
        closed = [p for p in self._positions.values() if p.status == "closed"]
        closed.sort(key=lambda p: p.closed_at or 0.0)
        return [_to_record(p) for p in closed]


def _to_held(pos: _MutablePosition) -> HeldPosition:
    return HeldPosition(
        position_id=pos.id,
        market_id=pos.market_id,
        side=pos.side,
        token_id=pos.token_id,
        condition_id=pos.condition_id,
        qty=pos.qty,
        avg_entry_price=pos.avg_entry_price,
        opened_at=pos.opened_at,
    )


def _to_record(pos: _MutablePosition) -> PositionRecord:
    return PositionRecord(
        id=pos.id,
        market_id=pos.market_id,
        side=pos.side,
        token_id=pos.token_id,
        condition_id=pos.condition_id,
        qty=pos.qty,
        avg_entry_price=pos.avg_entry_price,
        status=pos.status,  # type: ignore[arg-type]
        opened_at=pos.opened_at,
        closed_at=pos.closed_at,
        close_reason=pos.close_reason,
        realized_pnl=pos.realized_pnl,
    )
