"""News confluence — deriving a position's regime from its attached signals.

A position accumulates ``PositionSignal`` rows: the news that opened it, every
later headline that argued for the same side (``reinforce``), and every one
that argued for the other side of the same market (``contradict``). This module
folds that ledger into one of three states:

    solo        only the opening news still counts
    reinforced  at least ``reinforce_min_count`` same-side signals are live
    contested   an opposing signal has joined, ever

``contested`` dominates: once the pipeline has argued both directions on one
market, the thesis is disputed and no amount of later agreement un-disputes it.

Two asymmetries are deliberate:

- **Support decays, contradiction latches.** A supporting headline only counts
  inside ``support_ttl_seconds``. Without that, one duplicate headline in the
  first minute would hold a position's drawdown protection off for the rest of
  its life. A contradiction has no TTL — it is a statement about the thesis,
  not about the current news flow.
- **The opening signal decays too.** That is what lets a position drift back
  from ``reinforced`` to ``solo`` as its news ages, rather than latching on the
  strength of stale agreement.

Pure: no DB, no clock. ``now`` is passed in — the exit monitor passes its tick
timestamp, the backtest passes the replayed snapshot's timestamp, and the read
API passes wall-clock. Signals stamped **after** ``now`` are ignored, which is
what makes backtest replay lookahead-free by construction: ``_replay_exits``
records every entry before it evaluates any exit tick, so without that filter a
headline from three hours later would steer a mark from t+0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from openpoly.portfolio.models import ConfluenceState, PositionSignal, Side

# Ordering for the ``min_confidence`` floor — mirrors the analyzer's own
# Confidence literal (openpoly.sections.analyzer.llm_v0).
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_DEFAULT_RANK = 0  # an absent / unrecognized confidence is treated as "low"


@dataclass(frozen=True)
class Confluence:
    """The derived regime plus the counts it was derived from — the counts are
    carried so the exit section can apply ``contested_close_after`` and so the
    UI can explain *why* a position is in the state it's in."""

    state: ConfluenceState
    support: int
    against: int


def _counts(confidence: str | None, floor_rank: int) -> bool:
    return _CONFIDENCE_RANK.get(confidence or "", _DEFAULT_RANK) >= floor_rank


def evaluate(
    signals: Sequence[PositionSignal],
    *,
    position_side: Side,
    now: float,
    support_ttl_seconds: float = 0.0,
    reinforce_min_count: int = 2,
    min_confidence: str = "low",
) -> Confluence:
    """Fold a position's signal ledger into a ``Confluence``.

    ``position_side`` is the side actually held. A signal is supporting when it
    wanted that side and opposing when it wanted the other — the stored
    ``relation`` is not trusted for this, because it was assigned at execution
    time and side is the durable fact. ``support_ttl_seconds`` of 0 disables
    decay entirely (every supporting signal counts forever).
    """
    floor_rank = _CONFIDENCE_RANK.get(min_confidence, _DEFAULT_RANK)
    support = 0
    against = 0
    for sig in signals:
        if sig.ts > now:
            continue  # not yet known at the mark — see module docstring
        if not _counts(sig.confidence, floor_rank):
            continue
        if sig.side == position_side:
            if support_ttl_seconds > 0 and now - sig.ts > support_ttl_seconds:
                continue
            support += 1
        else:
            against += 1

    if against >= 1:
        state: ConfluenceState = "contested"
    elif support >= reinforce_min_count:
        state = "reinforced"
    else:
        state = "solo"
    return Confluence(state=state, support=support, against=against)
