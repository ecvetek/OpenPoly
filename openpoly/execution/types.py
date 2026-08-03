"""Shared types for the execution layer — keeps ExecResult import-cycle-free
between PaperExecutor and ExecutorDispatcher."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecResult:
    """Outcome of an executor call. On ``filled`` the ``price`` / ``qty`` /
    ``position_id`` fields are set; on a skip ``skip_reason`` carries a stable
    label, ``price``/``qty`` stay None, and ``position_id`` is set only for
    ``"position_exists"`` — the id of the blocking position."""

    filled: bool
    skip_reason: str | None = None
    price: float | None = None
    qty: float | None = None
    position_id: int | None = None

    @classmethod
    def skip(cls, reason: str, *, position_id: int | None = None) -> "ExecResult":
        return cls(filled=False, skip_reason=reason, position_id=position_id)

    @classmethod
    def ok(cls, *, price: float, qty: float, position_id: int) -> "ExecResult":
        return cls(filled=True, price=price, qty=qty, position_id=position_id)
