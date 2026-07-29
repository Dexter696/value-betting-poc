from datetime import datetime, timedelta, timezone

from vb.episode import EpisodeTracker, LegReadingV2
from vb.feature_dataset import build_feature_dataset, build_feature_row
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
    SignalObservation,
    SourceRun,
    StrategyDefinition,
)
from vb.pipeline import market_key
from vb.settlement_evidence import get_or_create_canonical_event, record_result_evidence, record_settlement_version
from vb.storage import (
    finish_source_run,
    get_or_create_strategy_definition,
    init_db,
    list_all_signal_observations,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_source_run,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
BENCHMARK_EVENT = RawEvent(
    site="pinnacle.com", sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
    raw_home_team="Liverpool", raw_away_team="Everton", event_id="p1",
)
MARKET_IDENTITY = market_key(BENCHMARK_EVENT, MarketType.MATCH_WINNER, None, Selection.HOME, "swisslos.ch")


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _strategy_id(conn):
    config = {"threshold": 0.03}
    s = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=0.03, max_age_s=90, max_skew_s=60,
        min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0,
    )
    return get_or_create_strategy_definition(conn, s)


def _snapshot(conn, at, site, event_id, home_odds, draw_odds=3.40, away_odds=3.60):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=at, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site=site, mode="quick", started_at=at))
    finish_source_run(conn, source_run_id, RunStatus.SUCCESS, at, event_count=1, snapshot_count=1)
    event_version_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=event_version_id, site=site, event_id=event_id, valid_from=at, source_run_id=source_run_id,
        sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
        home_team="Liverpool", away_team="Everton",
    ))
    snap_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snap_id, source_run_id=source_run_id, event_version_id=event_version_id, market_type=MarketType.MATCH_WINNER,
        line=None, outcomes=(Outcome(Selection.HOME, home_odds), Outcome(Selection.DRAW, draw_odds), Outcome(Selection.AWAY, away_odds)),
        received_at=at,
    ))
    return snap_id, event_version_id


def test_build_feature_row_returns_none_when_the_observation_has_no_episode():
    obs = SignalObservation(
        id="obs-1", episode_id=None, decision_time=T0, benchmark_snapshot_id="b", comparison_snapshot_id="c",
        edge_model="raw-v1", edge=0.05, eligible=True,
    )
    # no conn access needed - the episode_id check short-circuits first
    assert build_feature_row(conn=None, observation=obs) is None


def test_build_feature_row_computes_fair_probabilities_from_the_benchmark_market_only(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    bench_snap, _ = _snapshot(conn, T0, "pinnacle.com", "p1", home_odds=2.10)
    comp_snap, _ = _snapshot(conn, T0, "swisslos.ch", "s1", home_odds=2.30)
    result = tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))
    assert result.opened

    observations = list_all_signal_observations(conn)
    assert len(observations) == 1
    feature_row = build_feature_row(conn, observations[0])

    assert feature_row is not None
    assert feature_row.market_identity_id == MARKET_IDENTITY
    assert feature_row.strategy_version == strategy_id
    assert set(feature_row.fair_probabilities.keys()) == {"proportional", "power", "odds_ratio"}
    # proportional de-vig of (2.10, 3.40, 3.60) for HOME - known value
    assert abs(feature_row.fair_probabilities["proportional"] - 0.4543429844097996) < 1e-9
    assert feature_row.fair_probability_dispersion is not None
    assert feature_row.fair_probability_dispersion >= 0.0
    assert feature_row.settlement_result is None  # never settled in this test


def test_build_feature_row_includes_settlement_result_once_the_leg_is_settled(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    bench_snap, bench_ev_id = _snapshot(conn, T0, "pinnacle.com", "p1", home_odds=2.10)
    comp_snap, _ = _snapshot(conn, T0, "swisslos.ch", "s1", home_odds=2.30)
    tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))

    canonical_id = get_or_create_canonical_event(conn, bench_ev_id, sport="soccer", now=T0 + timedelta(hours=6))
    evidence_id = record_result_evidence(conn, canonical_id, provider="espn", retrieved_at=T0 + timedelta(hours=6), status="final", home_goals=2, away_goals=1)
    record_settlement_version(
        conn, canonical_id, MarketType.MATCH_WINNER, None, Selection.HOME, evidence_id,
        home_goals=2, away_goals=1, created_at=T0 + timedelta(hours=6),
    )

    observations = list_all_signal_observations(conn)
    feature_row = build_feature_row(conn, observations[0])

    assert feature_row.settlement_result == "won"  # Liverpool (home) won 2-1


def test_build_feature_dataset_batches_across_multiple_episodes(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)

    tracker_a = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)
    bench_a, _ = _snapshot(conn, T0, "pinnacle.com", "p1", home_odds=2.10)
    comp_a, _ = _snapshot(conn, T0, "swisslos.ch", "s1", home_odds=2.30)
    tracker_a.ingest(LegReadingV2(received_at=T0, edge=0.05, benchmark_snapshot_id=bench_a, comparison_snapshot_id=comp_a, edge_model="raw-v1"))

    other_identity = market_key(BENCHMARK_EVENT, MarketType.MATCH_WINNER, None, Selection.AWAY, "swisslos.ch")
    tracker_b = EpisodeTracker(conn, strategy_id, other_identity, threshold=0.03)
    bench_b, _ = _snapshot(conn, T0, "pinnacle.com", "p1", home_odds=2.10, away_odds=3.20)
    comp_b, _ = _snapshot(conn, T0, "swisslos.ch", "s1", home_odds=2.30, away_odds=3.40)
    tracker_b.ingest(LegReadingV2(received_at=T0, edge=0.05, benchmark_snapshot_id=bench_b, comparison_snapshot_id=comp_b, edge_model="raw-v1"))

    dataset = build_feature_dataset(conn)

    assert len(dataset) == 2
    assert {row.market_identity_id for row in dataset} == {MARKET_IDENTITY, other_identity}


def test_build_feature_dataset_respects_limit(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)
    bench, _ = _snapshot(conn, T0, "pinnacle.com", "p1", home_odds=2.10)
    comp, _ = _snapshot(conn, T0, "swisslos.ch", "s1", home_odds=2.30)
    tracker.ingest(LegReadingV2(received_at=T0, edge=0.05, benchmark_snapshot_id=bench, comparison_snapshot_id=comp, edge_model="raw-v1"))

    dataset = build_feature_dataset(conn, limit=0)
    assert dataset == []
