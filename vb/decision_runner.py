"""Wires vb.strategy's entry-policy state machines to real episodes —
closing the loop the shadow pipeline (vb.pipeline.run_cycle_v2) left
open: an episode being tracked was never actually turned into a
bet_decision/bet_execution. Without this, Phase 6's evaluation_v2 has
nothing real to evaluate no matter how long the shadow pipeline runs.

For every episode a run_cycle_v2() call touched this cycle, evaluates
`entry_policy` over the episode's full observation history and, on a
BET decision, records it (idempotent — safe to call every cycle even
though most calls will find the decision already made) and verifies it
against the latest known odds for that leg.

`fetch_current_odds` (vb.execution.verify_and_execute's re-verification
step) deliberately does NOT re-derive "current" odds via a fresh live
scrape — there isn't one available mid-cycle. It looks up the most
recent SUCCESS-run v2 snapshot for the exact same (event_version,
market_type, line) the triggering observation used
(vb.storage.find_latest_snapshot_for_event_version). In shadow mode,
right after the triggering cycle, this is usually the identical
snapshot (nothing fresher exists yet); once enough cycles have run
since the trigger, it becomes a genuine re-check against whatever's
been captured since. This is an honest reflection of what's actually
knowable right now, not a simulation of latency that doesn't exist yet
— see scripts/scheduled_run.py's SHADOW_FRESHNESS_LIMITS note on the
same theme.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from .episode import EpisodeIngestResult
from .execution import LatencyModel, record_decision, verify_and_execute
from .models import BetDecisionChoice, BetExecution
from .pipeline import parse_market_identity
from .storage import (
    find_latest_snapshot_for_event_version,
    list_signal_observations,
    load_market_snapshot_v2,
    load_signal_episode,
)
from .strategy import EntryPolicy, EntryPolicyState, ObservationForPolicy

# Shadow mode has no real placement latency to model - see this
# module's docstring. Kept as an explicit, named, non-zero value
# (rather than timedelta(0)) so responded_at is still meaningfully
# distinct from requested_at in stored data, and so this is a single
# obvious place to change once real execution latency exists.
SHADOW_LATENCY = LatencyModel(delay=timedelta(seconds=1))


def process_episode_decision(conn, result: EpisodeIngestResult, entry_policy: EntryPolicy) -> Optional[BetExecution]:
    """Evaluate `entry_policy` over one touched episode's full
    observation history; record and verify/execute a decision if the
    policy says BET. Returns the BetExecution if a NEW decision was
    made this call, None if the episode has no id, the policy is still
    WAITING/ABANDONED, or a decision for this (strategy_version,
    market_identity_id) pair already existed (idempotent - see
    vb.execution.record_decision).
    """
    if result.episode_id is None:
        return None

    episode = load_signal_episode(conn, result.episode_id)
    if episode is None:
        return None

    raw_observations = list_signal_observations(conn, episode.id)
    if not raw_observations:
        return None

    policy_observations = [
        ObservationForPolicy(
            decision_time=o.decision_time, edge=o.edge, eligible=o.eligible, observation_id=o.id,
        )
        for o in raw_observations
    ]
    decision = entry_policy.evaluate(policy_observations, as_of=raw_observations[-1].decision_time)
    if decision.state != EntryPolicyState.DECIDED or decision.decision != BetDecisionChoice.BET:
        return None

    trigger_observation = next(o for o in raw_observations if o.id == decision.trigger_observation_id)
    comparison_snapshot = load_market_snapshot_v2(conn, trigger_observation.comparison_snapshot_id)
    if comparison_snapshot is None:
        return None

    parsed = parse_market_identity(episode.market_identity_id)
    requested_odds = next(o.odds for o in comparison_snapshot.outcomes if o.selection == parsed.selection)

    decision_id = record_decision(
        conn, strategy_version=episode.strategy_version, market_identity_id=episode.market_identity_id,
        signal_observation_id=trigger_observation.id, decided_at=trigger_observation.decision_time,
        decision=BetDecisionChoice.BET, reason=decision.reason, intended_odds=requested_odds, intended_stake=1.0,
    )
    if decision_id is None:
        return None  # already decided on a previous cycle - nothing new to execute

    def fetch_current_odds() -> Optional[float]:
        latest = find_latest_snapshot_for_event_version(
            conn, comparison_snapshot.event_version_id, parsed.market_type, parsed.line,
        )
        if latest is None:
            return None
        return next((o.odds for o in latest.outcomes if o.selection == parsed.selection), None)

    return verify_and_execute(
        conn, decision_id, requested_at=trigger_observation.decision_time, requested_odds=requested_odds,
        requested_stake=1.0, fetch_current_odds=fetch_current_odds, latency=SHADOW_LATENCY,
    )


def process_cycle_decisions(conn, results: list[EpisodeIngestResult], entry_policy: EntryPolicy) -> list[BetExecution]:
    """process_episode_decision() over every episode a run_cycle_v2()
    call touched this cycle - skips readings that never opened/touched
    an episode at all (episode_id is None) without erroring."""
    executions = []
    for result in results:
        execution = process_episode_decision(conn, result, entry_policy)
        if execution is not None:
            executions.append(execution)
    return executions
