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
from .exposure import ExposureLimits, ExposurePosition, check_exposure
from .models import BetDecisionChoice, BetExecution, ExecutionStatus
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


def _current_exposure_positions(conn) -> list[ExposurePosition]:
    """Every currently-accepted (ACCEPTED or PRICE_CHANGED) bet's stake,
    resolved back to (event_id, site) via its episode's
    market_identity_id - what vb.exposure.check_exposure needs to
    decide whether a NEW bet would push concentration over the
    configured limits. Queried fresh on every call rather than cached,
    matching this module's stateless-by-construction pattern (same
    reasoning as vb.episode.EpisodeTracker - safe to call from a fresh
    process every cycle rather than needing to track state across
    calls).
    """
    rows = conn.execute(
        """
        SELECT be.accepted_stake, se.market_identity_id
        FROM bet_execution be
        JOIN bet_decision bd ON bd.id = be.decision_id
        JOIN signal_observation so ON so.id = bd.signal_observation_id
        JOIN signal_episode se ON se.id = so.episode_id
        WHERE be.status IN (?, ?) AND be.accepted_stake IS NOT NULL
        """,
        (ExecutionStatus.ACCEPTED.value, ExecutionStatus.PRICE_CHANGED.value),
    ).fetchall()
    positions = []
    for stake, market_identity_id in rows:
        parsed = parse_market_identity(market_identity_id)
        positions.append(ExposurePosition(event_id=parsed.benchmark_event_id, site=parsed.comparison_site, stake=stake))
    return positions


def process_episode_decision(
    conn, result: EpisodeIngestResult, entry_policy: EntryPolicy, limits: Optional[ExposureLimits] = None,
) -> Optional[BetExecution]:
    """Evaluate `entry_policy` over one touched episode's full
    observation history; record and verify/execute a decision if the
    policy says BET. Returns the BetExecution if a NEW decision was
    made this call, None if the episode has no id, the policy is still
    WAITING/ABANDONED, a decision for this (strategy_version,
    market_identity_id) pair already existed (idempotent - see
    vb.execution.record_decision), or `limits` is given and placing
    this bet would push either event- or site-level exposure over it.

    An exposure-blocked bet deliberately does NOT call record_decision
    at all - exposure is a transient condition (an existing position
    can close and free up room), unlike "already decided," so the next
    cycle gets a genuine chance to retry rather than being permanently
    blocked by having already consumed the idempotency key.
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

    if limits is not None:
        current_positions = _current_exposure_positions(conn)
        exposure = check_exposure(
            current_positions, parsed.benchmark_event_id, parsed.comparison_site, candidate_stake=1.0, limits=limits,
        )
        if not exposure.allowed:
            return None

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


def process_cycle_decisions(
    conn, results: list[EpisodeIngestResult], entry_policy: EntryPolicy, limits: Optional[ExposureLimits] = None,
) -> list[BetExecution]:
    """process_episode_decision() over every episode a run_cycle_v2()
    call touched this cycle - skips readings that never opened/touched
    an episode at all (episode_id is None) without erroring."""
    executions = []
    for result in results:
        execution = process_episode_decision(conn, result, entry_policy, limits)
        if execution is not None:
            executions.append(execution)
    return executions
