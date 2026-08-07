"""Tests for ``openpoly.portfolio.confluence.evaluate`` — the pure fold from a
position's signal ledger to its regime.

No DB, no clock: every case pins ``now`` explicitly. The two asymmetries the
module exists to encode (support decays / contradiction latches) and the
``ts > now`` lookahead guard are what these cover.
"""

from __future__ import annotations

from openpoly.portfolio.confluence import evaluate
from openpoly.portfolio.models import PositionSignal

HOUR = 3600.0
NOW = 100_000.0


def _sig(
    sid: int,
    side: str,
    ts: float,
    *,
    relation: str = "reinforce",
    confidence: str = "high",
) -> PositionSignal:
    return PositionSignal(
        id=sid,
        position_id=1,
        news_id=f"n{sid}",
        ts=ts,
        side=side,  # type: ignore[arg-type]
        relation=relation,  # type: ignore[arg-type]
        p_model=0.7,
        confidence=confidence,
    )


def test_opening_only_is_solo() -> None:
    c = evaluate(
        [_sig(1, "yes", NOW - 60, relation="opening")],
        position_side="yes",
        now=NOW,
    )
    assert (c.state, c.support, c.against) == ("solo", 1, 0)


def test_no_signals_is_solo() -> None:
    c = evaluate([], position_side="yes", now=NOW)
    assert (c.state, c.support, c.against) == ("solo", 0, 0)


def test_two_same_side_is_reinforced() -> None:
    c = evaluate(
        [_sig(1, "yes", NOW - 600, relation="opening"), _sig(2, "yes", NOW - 300)],
        position_side="yes",
        now=NOW,
    )
    assert (c.state, c.support) == ("reinforced", 2)


def test_reinforce_min_count_is_respected() -> None:
    sigs = [_sig(1, "yes", NOW - 600, relation="opening"), _sig(2, "yes", NOW - 300)]
    c = evaluate(sigs, position_side="yes", now=NOW, reinforce_min_count=3)
    assert c.state == "solo"


def test_opposite_side_signal_contests() -> None:
    c = evaluate(
        [_sig(1, "yes", NOW - 600, relation="opening"), _sig(2, "no", NOW - 60)],
        position_side="yes",
        now=NOW,
    )
    assert (c.state, c.support, c.against) == ("contested", 1, 1)


def test_contested_dominates_reinforced() -> None:
    """Even five same-side confirmations do not un-dispute a thesis."""
    sigs = [
        _sig(i, "yes", NOW - 600 + i, relation="opening" if i == 1 else "reinforce")
        for i in range(1, 6)
    ]
    sigs.append(_sig(9, "no", NOW - 30))
    c = evaluate(sigs, position_side="yes", now=NOW)
    assert c.state == "contested"
    assert c.support == 5 and c.against == 1


def test_side_is_read_relative_to_the_held_side() -> None:
    """A NO position reinforced by NO news, contested by YES news — the mirror
    of the cases above. ``relation`` is not what decides support vs against."""
    sigs = [_sig(1, "no", NOW - 600, relation="opening"), _sig(2, "no", NOW - 300)]
    assert evaluate(sigs, position_side="no", now=NOW).state == "reinforced"
    sigs.append(_sig(3, "yes", NOW - 60))
    assert evaluate(sigs, position_side="no", now=NOW).state == "contested"


# ---------- decay ----------


def test_support_decays_out_of_the_ttl_window() -> None:
    sigs = [_sig(1, "yes", NOW - 3 * HOUR, relation="opening"), _sig(2, "yes", NOW - 2 * HOUR)]
    fresh = evaluate(sigs, position_side="yes", now=NOW - 2 * HOUR, support_ttl_seconds=2 * HOUR)
    assert fresh.state == "reinforced"
    # Two hours later the older one has aged out — one live signal left, so the
    # position drops back to solo and its drawdown protection re-tightens.
    partly = evaluate(sigs, position_side="yes", now=NOW, support_ttl_seconds=2 * HOUR)
    assert (partly.state, partly.support) == ("solo", 1)
    # Later still, both are gone.
    stale = evaluate(sigs, position_side="yes", now=NOW + HOUR, support_ttl_seconds=2 * HOUR)
    assert (stale.state, stale.support) == ("solo", 0)


def test_ttl_boundary_is_inclusive() -> None:
    """A signal exactly ``ttl`` old still counts; one second older does not."""
    sigs = [_sig(1, "yes", NOW - HOUR, relation="opening"), _sig(2, "yes", NOW - HOUR)]
    assert evaluate(sigs, position_side="yes", now=NOW, support_ttl_seconds=HOUR).support == 2
    assert evaluate(sigs, position_side="yes", now=NOW + 1, support_ttl_seconds=HOUR).support == 0


def test_ttl_zero_disables_decay() -> None:
    sigs = [_sig(1, "yes", NOW - 30 * HOUR, relation="opening"), _sig(2, "yes", NOW - 29 * HOUR)]
    c = evaluate(sigs, position_side="yes", now=NOW, support_ttl_seconds=0.0)
    assert c.state == "reinforced"


def test_contradiction_never_decays() -> None:
    sigs = [
        _sig(1, "yes", NOW - 30 * HOUR, relation="opening"),
        _sig(2, "no", NOW - 29 * HOUR, relation="contradict"),
    ]
    c = evaluate(sigs, position_side="yes", now=NOW, support_ttl_seconds=HOUR)
    # Support aged out; the contradiction did not.
    assert (c.state, c.support, c.against) == ("contested", 0, 1)


# ---------- confidence floor ----------


def test_min_confidence_filters_both_directions() -> None:
    sigs = [
        _sig(1, "yes", NOW - 600, relation="opening", confidence="high"),
        _sig(2, "yes", NOW - 300, confidence="low"),
        _sig(3, "no", NOW - 100, confidence="low"),
    ]
    counted_all = evaluate(sigs, position_side="yes", now=NOW, min_confidence="low")
    assert (counted_all.state, counted_all.support, counted_all.against) == ("contested", 2, 1)

    filtered = evaluate(sigs, position_side="yes", now=NOW, min_confidence="medium")
    assert (filtered.state, filtered.support, filtered.against) == ("solo", 1, 0)


def test_missing_confidence_ranks_as_low() -> None:
    sig = _sig(1, "yes", NOW - 60, relation="opening")
    unknown = PositionSignal(**{**sig.__dict__, "confidence": None})
    assert evaluate([unknown], position_side="yes", now=NOW, min_confidence="low").support == 1
    assert evaluate([unknown], position_side="yes", now=NOW, min_confidence="medium").support == 0


# ---------- lookahead guard ----------


def test_signals_stamped_after_now_are_ignored() -> None:
    """The backtest guard. ``_replay_exits`` runs after every entry has been
    replayed, so the ledger it hands the exit section contains signals from
    later than the snapshot being marked. Counting those would make the
    confluence rules look good for entirely fake reasons."""
    sigs = [
        _sig(1, "yes", NOW - 600, relation="opening"),
        _sig(2, "yes", NOW + 600),  # three hours into the position's future
        _sig(3, "no", NOW + 900),
    ]
    at_now = evaluate(sigs, position_side="yes", now=NOW)
    assert (at_now.state, at_now.support, at_now.against) == ("solo", 1, 0)

    # ... and once the clock reaches them, they count.
    later = evaluate(sigs, position_side="yes", now=NOW + 1000, support_ttl_seconds=0.0)
    assert (later.state, later.support, later.against) == ("contested", 2, 1)
