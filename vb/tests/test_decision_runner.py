from datetime import datetime, timedelta, timezone

from vb.decision_runner import process_cycle_decisions, process_episode_decision
from vb.episode import EpisodeTracker, LegReadingV2
from vb.exposure import ExposureLimits
from vb.identity import content_hash, new_id
from vb.models import (
    CaptureRun,
    EventVersionV2,
    MarketSnapshotV2,
    MarketType,
    Outcome,
    RawEvent,
    Selection,
    SourceRun,
    StrategyDefinition,
)
from vb.pipeline import market_key
from vb.models import RunStatus
from vb.storage import (
    finish_source_run,
    get_or_create_strategy_definition,
    init_db,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_source_run,
)
from vb.strategy import ImmediateEntryPolicy, PersistentEntryPolicy

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
BENCHMARK_EVENT = RawEvent(
    site="pinnacle.com", sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
    raw_home_team="Liverpool", raw_away_team="Everton", event_id="p1",
)
MARKET_IDENTITY = market_key(BENCHMARK_EVENT, MarketType.MATCH_WINNER, None, Selection.HOME, "swisslos.ch")


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _strategy_id(conn, threshold=0.03):
    config = {"threshold": threshold}
    s = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=threshold, max_age_s=90, max_skew_s=60,
        min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0,
    )
    return get_or_create_strategy_definition(conn, s)


def _snapshot(conn, at, comparison_odds, event_version_id=None):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=at, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site="swisslos.ch", mode="quick", started_at=at))
    finish_source_run(conn, source_run_id, RunStatus.SUCCESS, at, event_count=1, snapshot_count=1)
    if event_version_id is None:
        event_version_id = new_id()
        save_event_version(conn, EventVersionV2(
            id=event_version_id, site="swisslos.ch", event_id="s1", valid_from=at, source_run_id=source_run_id,
            sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
            home_team="Liverpool", away_team="Everton",
        ))
    snap_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snap_id, source_run_id=source_run_id, event_version_id=event_version_id, market_type=MarketType.MATCH_WINNER,
        line=None, outcomes=(Outcome(Selection.HOME, comparison_odds), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=at,
    ))
    return snap_id, event_version_id


def _open_episode(conn, strategy_id, at=T0, edge=0.05, comparison_odds=2.30):
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)
    bench_snap, _ = _snapshot(conn, at, 2.0)  # benchmark side snapshot id unused by decision_runner directly
    comp_snap, event_version_id = _snapshot(conn, at, comparison_odds)
    reading = LegReadingV2(
        received_at=at, edge=edge, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap,
        edge_model="raw-v1",
    )
    result = tracker.ingest(reading)
    return result, event_version_id


def test_immediate_policy_records_and_executes_on_the_triggering_observation(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    result, _ = _open_episode(conn, strategy_id, comparison_odds=2.30)

    execution = process_episode_decision(conn, result, ImmediateEntryPolicy(threshold=0.03))

    assert execution is not None
    assert execution.requested_odds == 2.30
    assert execution.accepted_odds == 2.30  # no fresher snapshot exists yet - same price

    decision_row = conn.execute("SELECT decision, intended_odds FROM bet_decision").fetchone()
    assert decision_row == ("bet", 2.30)


def test_a_second_call_for_the_same_episode_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    result, _ = _open_episode(conn, strategy_id, comparison_odds=2.30)
    policy = ImmediateEntryPolicy(threshold=0.03)

    first = process_episode_decision(conn, result, policy)
    second = process_episode_decision(conn, result, policy)

    assert first is not None
    assert second is None
    assert conn.execute("SELECT COUNT(*) FROM bet_decision").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bet_execution").fetchone()[0] == 1


def test_persistent_policy_returns_none_while_still_waiting(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    result, _ = _open_episode(conn, strategy_id, edge=0.05, comparison_odds=2.30)

    execution = process_episode_decision(conn, result, PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5)))

    assert execution is None
    assert conn.execute("SELECT COUNT(*) FROM bet_decision").fetchone()[0] == 0


def test_returns_none_for_a_result_with_no_episode_id(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)
    bench_snap, _ = _snapshot(conn, T0, 2.0)
    comp_snap, _ = _snapshot(conn, T0, 2.02)  # edge below threshold - never opens
    reading = LegReadingV2(received_at=T0, edge=0.01, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1")
    result = tracker.ingest(reading)

    execution = process_episode_decision(conn, result, ImmediateEntryPolicy(threshold=0.03))

    assert execution is None


def test_fetch_current_odds_reflects_a_fresher_snapshot_captured_after_the_trigger(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    result, event_version_id = _open_episode(conn, strategy_id, at=T0, comparison_odds=2.30)

    # a later capture cycle for the SAME event/market recorded a worse price
    _snapshot(conn, T0 + timedelta(minutes=5), comparison_odds=2.10, event_version_id=event_version_id)

    execution = process_episode_decision(conn, result, ImmediateEntryPolicy(threshold=0.03))

    assert execution is not None
    assert execution.requested_odds == 2.30
    assert execution.accepted_odds == 2.10  # picked up the fresher, worse price


def test_process_cycle_decisions_handles_a_mixed_batch(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    opened_result, _ = _open_episode(conn, strategy_id, comparison_odds=2.30)

    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY + ":other", threshold=0.03)
    bench_snap, _ = _snapshot(conn, T0, 2.0)
    comp_snap, _ = _snapshot(conn, T0, 2.02)
    below_threshold_result = tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.01, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))

    executions = process_cycle_decisions(conn, [opened_result, below_threshold_result], ImmediateEntryPolicy(threshold=0.03))

    assert len(executions) == 1


SECOND_MARKET_IDENTITY = market_key(BENCHMARK_EVENT, MarketType.MATCH_WINNER, None, Selection.AWAY, "swisslos.ch")


def test_exposure_limit_blocks_a_new_bet_that_would_exceed_event_stake(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    policy = ImmediateEntryPolicy(threshold=0.03)

    # first bet on this event: uses up the entire event-level allowance
    first_result, _ = _open_episode(conn, strategy_id, comparison_odds=2.30)
    first_execution = process_episode_decision(conn, first_result, policy)
    assert first_execution is not None

    # a second, independent episode on the SAME real event (different
    # selection) - a live production run_cycle_v2() would legitimately
    # produce more than one leg per event
    tracker = EpisodeTracker(conn, strategy_id, SECOND_MARKET_IDENTITY, threshold=0.03)
    bench_snap, _ = _snapshot(conn, T0, 2.0)
    comp_snap, _ = _snapshot(conn, T0, 2.30)
    second_result = tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))

    limits = ExposureLimits(max_stake_per_event=1.0, max_stake_per_site=10.0)
    second_execution = process_episode_decision(conn, second_result, policy, limits=limits)

    assert second_execution is None
    # blocked BEFORE recording - the idempotency key was never consumed,
    # so a later cycle (once exposure frees up) can still decide this
    assert conn.execute("SELECT COUNT(*) FROM bet_decision").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bet_execution").fetchone()[0] == 1


def test_exposure_limit_allows_a_bet_within_the_configured_ceiling(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    policy = ImmediateEntryPolicy(threshold=0.03)

    result, _ = _open_episode(conn, strategy_id, comparison_odds=2.30)
    limits = ExposureLimits(max_stake_per_event=10.0, max_stake_per_site=10.0)

    execution = process_episode_decision(conn, result, policy, limits=limits)

    assert execution is not None
    assert conn.execute("SELECT COUNT(*) FROM bet_decision").fetchone()[0] == 1
