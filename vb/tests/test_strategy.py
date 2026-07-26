from datetime import datetime, timedelta, timezone

from vb.models import BetDecisionChoice
from vb.strategy import (
    EntryPolicyState,
    ImmediateEntryPolicy,
    ObservationForPolicy,
    PersistentEntryPolicy,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _obs(minute, edge, eligible=True, obs_id=None):
    return ObservationForPolicy(
        decision_time=T0 + timedelta(minutes=minute), edge=edge, eligible=eligible,
        observation_id=obs_id or f"obs-{minute}",
    )


def test_immediate_waits_when_nothing_has_crossed_yet():
    policy = ImmediateEntryPolicy(threshold=0.03)
    result = policy.evaluate([_obs(0, 0.01), _obs(1, 0.02)], as_of=T0 + timedelta(minutes=1))
    assert result.state == EntryPolicyState.WAITING


def test_immediate_decides_on_the_first_eligible_crossing():
    policy = ImmediateEntryPolicy(threshold=0.03)
    observations = [_obs(0, 0.01), _obs(1, 0.05, obs_id="the-trigger"), _obs(2, 0.09)]
    result = policy.evaluate(observations, as_of=T0 + timedelta(minutes=2))

    assert result.state == EntryPolicyState.DECIDED
    assert result.decision == BetDecisionChoice.BET
    assert result.trigger_observation_id == "the-trigger"  # the FIRST crossing, not the highest-edge one


def test_immediate_skips_an_ineligible_reading_even_with_a_high_edge():
    policy = ImmediateEntryPolicy(threshold=0.03)
    observations = [_obs(0, 0.09, eligible=False), _obs(1, 0.05, obs_id="real-trigger")]
    result = policy.evaluate(observations, as_of=T0 + timedelta(minutes=1))

    assert result.trigger_observation_id == "real-trigger"


def test_persistent_decides_once_the_edge_has_genuinely_held_for_the_full_window():
    policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    observations = [
        _obs(0, 0.05),                       # first crossing
        _obs(2, 0.04),
        _obs(4, 0.06),
        _obs(5, 0.05, obs_id="the-trigger"),  # exactly 5 minutes after first crossing
    ]
    result = policy.evaluate(observations, as_of=T0 + timedelta(minutes=5))

    assert result.state == EntryPolicyState.DECIDED
    assert result.trigger_observation_id == "the-trigger"


def test_persistent_is_still_waiting_before_the_window_elapses():
    policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    observations = [_obs(0, 0.05), _obs(3, 0.04)]
    result = policy.evaluate(observations, as_of=T0 + timedelta(minutes=3))
    assert result.state == EntryPolicyState.WAITING


def test_persistent_abandons_if_the_edge_drops_before_the_window_elapses():
    policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    observations = [_obs(0, 0.05), _obs(2, 0.06), _obs(3, 0.01)]  # drops below threshold at minute 3
    result = policy.evaluate(observations, as_of=T0 + timedelta(minutes=3))

    assert result.state == EntryPolicyState.ABANDONED
    assert result.decision is None


def test_persistent_ignores_an_ineligible_gap_instead_of_treating_it_as_a_break():
    # A stale/skewed reading in the middle of an otherwise-solid
    # persistence window carries no real price information - it must
    # not reset the clock the way a genuine below-threshold reading
    # would.
    policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    observations = [
        _obs(0, 0.05),
        _obs(2, 0.0, eligible=False),  # would look like a drop, but it's ineligible - ignored
        _obs(5, 0.05, obs_id="the-trigger"),
    ]
    result = policy.evaluate(observations, as_of=T0 + timedelta(minutes=5))

    assert result.state == EntryPolicyState.DECIDED
    assert result.trigger_observation_id == "the-trigger"


def test_persistent_never_crossed_stays_waiting():
    policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    result = policy.evaluate([_obs(0, 0.01), _obs(5, 0.02)], as_of=T0 + timedelta(minutes=5))
    assert result.state == EntryPolicyState.WAITING


def test_f05_replaying_observations_one_at_a_time_matches_seeing_them_all_at_once():
    # The audit's F-05 finding was that the old convergence-time filter
    # used the FINAL observed duration - a number only knowable once the
    # whole episode's future is known. The real fix is that this policy
    # must produce the exact same decision whether it's fed the full
    # history at once, or called fresh every pipeline cycle with only
    # what's been observed SO FAR (the real, live operating mode) - i.e.
    # no dependency on ever having seen "the future" relative to as_of.
    full_history = [_obs(0, 0.05), _obs(2, 0.04), _obs(4, 0.06), _obs(5, 0.05, obs_id="the-trigger"), _obs(9, 0.20)]

    live_policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    seen_so_far: list[ObservationForPolicy] = []
    live_result = None
    for obs in full_history:
        seen_so_far.append(obs)
        live_result = live_policy.evaluate(seen_so_far, as_of=obs.decision_time)
        if live_result.state != EntryPolicyState.WAITING:
            break

    hindsight_policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    hindsight_result = hindsight_policy.evaluate(full_history, as_of=full_history[-1].decision_time)

    assert live_result.state == EntryPolicyState.DECIDED
    assert live_result.trigger_observation_id == hindsight_result.trigger_observation_id == "the-trigger"
