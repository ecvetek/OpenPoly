"""Best-effort attachment of a news decision to a position.

Shared by ``PaperExecutor`` and ``LiveExecutor``, which both need to record the
same three relations (``opening`` on a fill, ``reinforce`` when a same-side
entry is blocked, ``contradict`` when an opposite-side entry is blocked) and
must both treat the write as non-fatal.

Non-fatal is the whole point of this module existing. On the live path the
``opening`` signal is written *after* an irreversible on-chain fill: raising
there would turn a completed trade into an ``error`` verdict. On the blocked
paths, raising would lose the blocking ``position_id`` that ``NewsCard``'s
deep-link and ``entry_decision.position_id`` depend on. A missing signal row
only degrades the position to a weaker confluence state, which is the safe
direction — it can never loosen a drawdown threshold.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openpoly.portfolio import PortfolioStore, Relation, Side

logger = logging.getLogger(__name__)


def attach_signal(
    store: "PortfolioStore",
    position_id: int,
    *,
    news_id: str | None,
    ts: float,
    side: "Side",
    relation: "Relation",
    p_model: float | None = None,
    confidence: str | None = None,
) -> None:
    """Record one news decision against ``position_id``. Never raises.

    A ``None`` ``news_id`` (a manually-opened paper position, or a fill with no
    upstream news) is skipped rather than stored — the ledger joins back to
    ``news_item`` / ``analyzer_call`` by that id, so a row without one carries
    no information.
    """
    if news_id is None:
        return
    try:
        store.record_signal(
            position_id,
            news_id=news_id,
            ts=ts,
            side=side,
            relation=relation,
            p_model=p_model,
            confidence=confidence,
        )
    except Exception:  # noqa: BLE001 — must never break a fill; see module docstring
        logger.exception(
            "record_signal(%s) failed for position %d; suppressing", relation, position_id
        )
