"""Entry-policy transition reporting — Phase 5 step 2 of the audit's
remediation roadmap (2026-07-25).

The audit asked to "store every observation, transition, and reject
reason." Observations and reject reasons are already stored
unconditionally (vb.episode's F-01 discipline — every reading, eligible
or not, becomes a signal_observation row). This module covers the
"transition" half WITHOUT a new schema table: an entry policy's
WAITING/DECIDED/ABANDONED state at any point is a pure function of an
episode's own observation history (vb.strategy.EntryPolicy.evaluate()),
so it's fully reconstructible on demand rather than needing its own
insert-only log — a schema addition would just be recording the same
information the observations already contain, in a different shape.

Genuinely deferred, not silently dropped: the same could be said before
an audit trail existed for OBSERVATIONS too, and the audit still asked
for that to be explicit. If a future need arises for a real
insert-only transition log (e.g. auditing exactly when a policy's
verdict was first computed, not just what it currently evaluates to),
that's a schema change to make deliberately then — this module is the
inexpensive, no-schema-risk version of the same information.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .strategy import EntryPolicy, EntryPolicyResult, EntryPolicyState, ObservationForPolicy
from .storage import list_signal_observations, load_signal_episode


def entry_policy_status_for_episode(conn, episode_id: str, entry_policy: EntryPolicy) -> Optional[EntryPolicyResult]:
    """What `entry_policy` currently evaluates to for one episode, given
    its full real observation history to date. None if the episode
    doesn't exist or has no observations at all yet.
    """
    episode = load_signal_episode(conn, episode_id)
    if episode is None:
        return None

    raw_observations = list_signal_observations(conn, episode_id)
    if not raw_observations:
        return None

    policy_observations = [
        ObservationForPolicy(decision_time=o.decision_time, edge=o.edge, eligible=o.eligible, observation_id=o.id)
        for o in raw_observations
    ]
    return entry_policy.evaluate(policy_observations, as_of=raw_observations[-1].decision_time)


@dataclass(frozen=True)
class EntryPolicySummary:
    waiting: int
    decided: int
    abandoned: int

    @property
    def total(self) -> int:
        return self.waiting + self.decided + self.abandoned

    @property
    def decision_rate(self) -> Optional[float]:
        """Fraction of episodes that reached a real decision (BET),
        among those that didn't just stay WAITING forever (still-open
        episodes aren't a meaningful denominator - they haven't had
        their chance to resolve yet)."""
        resolved = self.decided + self.abandoned
        return self.decided / resolved if resolved else None


def summarize_entry_policy_outcomes(conn, strategy_version: str, entry_policy: EntryPolicy) -> EntryPolicySummary:
    """Across every episode ever opened under `strategy_version`, how
    many are currently WAITING (still open, not yet decided one way or
    the other), DECIDED (the policy found a real BET trigger), or
    ABANDONED (the policy's own conditions for giving up were met,
    e.g. persistent-Nm-v1's edge dropping before the window elapsed).
    """
    episode_ids = [
        row[0] for row in conn.execute(
            "SELECT id FROM signal_episode WHERE strategy_version = ?", (strategy_version,)
        ).fetchall()
    ]

    waiting = decided = abandoned = 0
    for episode_id in episode_ids:
        result = entry_policy_status_for_episode(conn, episode_id, entry_policy)
        if result is None:
            continue
        if result.state == EntryPolicyState.WAITING:
            waiting += 1
        elif result.state == EntryPolicyState.DECIDED:
            decided += 1
        elif result.state == EntryPolicyState.ABANDONED:
            abandoned += 1

    return EntryPolicySummary(waiting=waiting, decided=decided, abandoned=abandoned)
