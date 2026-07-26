"""Online entry-policy state machines — Phase 5 step 1 of the audit's
remediation roadmap (2026-07-25).

Goal (the audit's own wording): the simulated price must correspond to
the moment the strategy actually decided — not to whatever edge value
happened to look best in hindsight once the whole episode's trajectory
was already known. That hindsight leak is exactly what F-05 found in
the OLD convergence-time filter: it used the FINAL observed duration an
edge stayed above threshold, a number that is only knowable after the
episode has already closed, to decide whether an entry "counted" -
which is look-ahead bias, not a real entry rule a live system could
ever have executed on.

Two named entry policies, per the audit's roadmap:

- immediate-v1: bet on the first eligible observation whose edge clears
  the strategy's threshold. No waiting.
- persistent-5m-v1: require the edge to stay above threshold,
  continuously (ineligible observations are skipped, not counted as a
  break — a data-quality gap isn't evidence the edge went away), for a
  full 5 minutes measured from the first crossing, before deciding to
  bet.

Both are implemented as pure, stateless functions of the episode's own
observation history up to `as_of`: evaluate() only ever looks at
observations whose decision_time is <= as_of, and only ever fires a
DECIDED result once a REAL observation's own decision_time has reached
the required moment — never by computing "the edge stayed up for N
minutes" from data that includes anything after `as_of`. This is the
literal fix for F-05, and it also means these policies are safe to
reconstruct fresh on every pipeline cycle (no in-memory identity to
lose across a process restart), the same design principle that fixed
F-02 in vb.episode.EpisodeTracker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .models import BetDecisionChoice


class EntryPolicyState(str, Enum):
    WAITING = "waiting"
    DECIDED = "decided"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class ObservationForPolicy:
    """The minimal slice of a signal_observation an entry policy is
    allowed to see - deliberately narrow so a policy implementation can
    never accidentally reach for a field that only makes sense in
    hindsight (e.g. how the episode eventually ended)."""

    decision_time: datetime
    edge: float
    eligible: bool
    observation_id: str


@dataclass(frozen=True)
class EntryPolicyResult:
    state: EntryPolicyState
    decision: Optional[BetDecisionChoice] = None
    trigger_observation_id: Optional[str] = None
    reason: str = ""


class EntryPolicy:
    """Base contract. `observations` must already be filtered/sorted to
    only include decision_time <= as_of by the caller - evaluate()
    itself does not re-check this, so a caller accidentally passing a
    future observation would silently reintroduce look-ahead bias. See
    vb.tests.test_strategy's dedicated regression test for why this
    boundary matters enough to test explicitly.
    """

    name: str
    threshold: float

    def evaluate(self, observations: list[ObservationForPolicy], as_of: datetime) -> EntryPolicyResult:
        raise NotImplementedError


class ImmediateEntryPolicy(EntryPolicy):
    name = "immediate-v1"

    def __init__(self, threshold: float):
        self.threshold = threshold

    def evaluate(self, observations: list[ObservationForPolicy], as_of: datetime) -> EntryPolicyResult:
        for obs in observations:
            if obs.eligible and obs.edge >= self.threshold:
                return EntryPolicyResult(
                    state=EntryPolicyState.DECIDED, decision=BetDecisionChoice.BET,
                    trigger_observation_id=obs.observation_id, reason="first eligible crossing",
                )
        return EntryPolicyResult(state=EntryPolicyState.WAITING, reason="no eligible crossing yet")


class PersistentEntryPolicy(EntryPolicy):
    """persistent-Nm-v1: the edge must stay >= threshold, continuously
    across every ELIGIBLE observation in between, for a full
    `persistence` duration measured from the first crossing - real
    elapsed time between real observations, never a retrospective
    "it turned out to hold for N minutes" shortcut (F-05)."""

    def __init__(self, threshold: float, persistence: timedelta, name: str = "persistent-5m-v1"):
        self.threshold = threshold
        self.persistence = persistence
        self.name = name

    def evaluate(self, observations: list[ObservationForPolicy], as_of: datetime) -> EntryPolicyResult:
        first_crossing: Optional[ObservationForPolicy] = None
        for obs in observations:
            if not obs.eligible:
                # a stale/skewed reading carries no real price information -
                # skip it rather than treat it as evidence the edge broke
                continue
            if obs.edge < self.threshold:
                if first_crossing is not None:
                    return EntryPolicyResult(
                        state=EntryPolicyState.ABANDONED,
                        reason="edge dropped below threshold before the persistence window elapsed",
                    )
                continue
            if first_crossing is None:
                first_crossing = obs
            if obs.decision_time - first_crossing.decision_time >= self.persistence:
                return EntryPolicyResult(
                    state=EntryPolicyState.DECIDED, decision=BetDecisionChoice.BET,
                    trigger_observation_id=obs.observation_id,
                    reason=f"edge held >= threshold for {self.persistence}",
                )

        if first_crossing is None:
            return EntryPolicyResult(state=EntryPolicyState.WAITING, reason="no eligible crossing yet")
        return EntryPolicyResult(state=EntryPolicyState.WAITING, reason="crossing seen, persistence window not yet elapsed")
