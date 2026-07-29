from datetime import datetime, timedelta, timezone

from vb.entry_policy_report import entry_policy_status_for_episode, summarize_entry_policy_outcomes
from vb.episode import EpisodeTracker, LegReadingV2
from vb.identity import content_hash, new_id
from vb.models import (
    CaptureRun,
    EventVersionV2,
    MarketSnapshotV2,
    MarketType,
    Outcome,
    RawEvent,
    RunStatus,
    Selection,
    SourceRun,
    StrategyDefinition,
)
from vb.pipeline import market_key
from vb.storage import (
    finish_source_run,
    get_or_create_strategy_definition,
    init_db,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_source_run,
)
from vb.strategy import EntryPolicyState, ImmediateEntryPolicy, PersistentEntryPolicy

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
BENCHMARK_EVENT = RawEvent(
    site="pinnacle.com", sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
    raw_home_team="Liverpool", raw_away_team="Everton", event_id="p1",
)


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _strategy_id(conn):
    config = {"threshold": 0.03}
    s = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=0.03, max_age_s=90, max_skew_s=60,
        min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0,
    )
    return get_or_create_strategy_definition(conn, s)


def _snapshot(conn, at, odds):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=at, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site="swisslos.ch", mode="quick", started_at=at))
    finish_source_run(conn, source_run_id, RunStatus.SUCCESS, at, event_count=1, snapshot_count=1)
    event_version_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=event_version_id, site="swisslos.ch", event_id="s1", valid_from=at, source_run_id=source_run_id,
        sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
        home_team="Liverpool", away_team="Everton",
    ))
    snap_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snap_id, source_run_id=source_run_id, event_version_id=event_version_id, market_type=MarketType.MATCH_WINNER,
        line=None, outcomes=(Outcome(Selection.HOME, odds), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=at,
    ))
    return snap_id


def _market_identity(selection=Selection.HOME):
    return market_key(BENCHMARK_EVENT, MarketType.MATCH_WINNER, None, selection, "swisslos.ch")


def test_entry_policy_status_for_episode_returns_none_for_a_nonexistent_episode(tmp_path):
    conn = _db(tmp_path)
    assert entry_policy_status_for_episode(conn, "nope", ImmediateEntryPolicy(threshold=0.03)) is None


def test_entry_policy_status_for_episode_reflects_a_real_decided_episode(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, _market_identity(), threshold=0.03)
    bench = _snapshot(conn, T0, 2.0)
    comp = _snapshot(conn, T0, 2.30)
    result = tracker.ingest(LegReadingV2(received_at=T0, edge=0.05, benchmark_snapshot_id=bench, comparison_snapshot_id=comp, edge_model="raw-v1"))

    status = entry_policy_status_for_episode(conn, result.episode_id, ImmediateEntryPolicy(threshold=0.03))

    assert status.state == EntryPolicyState.DECIDED


def test_entry_policy_status_for_episode_reflects_an_abandoned_episode(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, _market_identity(), threshold=0.03)
    bench1 = _snapshot(conn, T0, 2.0)
    comp1 = _snapshot(conn, T0, 2.30)
    result1 = tracker.ingest(LegReadingV2(received_at=T0, edge=0.05, benchmark_snapshot_id=bench1, comparison_snapshot_id=comp1, edge_model="raw-v1"))

    bench2 = _snapshot(conn, T0 + timedelta(minutes=1), 2.0)
    comp2 = _snapshot(conn, T0 + timedelta(minutes=1), 2.02)
    tracker.ingest(LegReadingV2(
        received_at=T0 + timedelta(minutes=1), edge=0.01, benchmark_snapshot_id=bench2, comparison_snapshot_id=comp2, edge_model="raw-v1",
    ))

    policy = PersistentEntryPolicy(threshold=0.03, persistence=timedelta(minutes=5))
    status = entry_policy_status_for_episode(conn, result1.episode_id, policy)

    assert status.state == EntryPolicyState.ABANDONED


def test_summarize_entry_policy_outcomes_counts_across_all_episodes_for_a_strategy(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    policy = ImmediateEntryPolicy(threshold=0.03)

    # episode 1: decides immediately
    tracker_a = EpisodeTracker(conn, strategy_id, _market_identity(Selection.HOME), threshold=0.03)
    bench_a = _snapshot(conn, T0, 2.0)
    comp_a = _snapshot(conn, T0, 2.30)
    tracker_a.ingest(LegReadingV2(received_at=T0, edge=0.05, benchmark_snapshot_id=bench_a, comparison_snapshot_id=comp_a, edge_model="raw-v1"))

    # episode 2: never crosses threshold - still waiting
    tracker_b = EpisodeTracker(conn, strategy_id, _market_identity(Selection.AWAY), threshold=0.03)
    bench_b = _snapshot(conn, T0, 2.0)
    comp_b = _snapshot(conn, T0, 2.02)
    result_b = tracker_b.ingest(LegReadingV2(received_at=T0, edge=0.01, benchmark_snapshot_id=bench_b, comparison_snapshot_id=comp_b, edge_model="raw-v1"))
    assert result_b.episode_id is None  # never opened at all - not counted, no episode exists

    summary = summarize_entry_policy_outcomes(conn, strategy_id, policy)

    assert summary.decided == 1
    assert summary.waiting == 0
    assert summary.abandoned == 0
    assert summary.total == 1
    assert summary.decision_rate == 1.0
