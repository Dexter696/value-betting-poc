from datetime import datetime, timedelta, timezone

from vb.decision_runner import process_episode_decision
from vb.episode import EpisodeTracker, LegReadingV2
from vb.evaluation_runner import assemble_executed_bets, run_evaluation
from vb.identity import content_hash, new_id
from vb.models import (
    CaptureRun,
    EventVersionV2,
    ExecutionStatus,
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
from vb.settlement import SettlementResult
from vb.settlement_evidence import get_or_create_canonical_event, record_result_evidence, record_settlement_version
from vb.storage import (
    finish_source_run,
    get_or_create_strategy_definition,
    init_db,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_source_run,
)
from vb.strategy import ImmediateEntryPolicy

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


def _snapshot(conn, at, site, event_id, odds):
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
        line=None, outcomes=(Outcome(Selection.HOME, odds), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=at,
    ))
    return snap_id, event_version_id


def _real_decided_bet(conn, strategy_id):
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)
    bench_snap, bench_ev_id = _snapshot(conn, T0, "pinnacle.com", "p1", 2.10)
    comp_snap, _ = _snapshot(conn, T0, "swisslos.ch", "s1", 2.30)
    result = tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))
    execution = process_episode_decision(conn, result, ImmediateEntryPolicy(threshold=0.03))
    return execution, bench_ev_id


def test_assemble_executed_bets_resolves_a_real_decided_and_executed_bet(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    execution, _ = _real_decided_bet(conn, strategy_id)
    assert execution is not None

    bets = assemble_executed_bets(conn, strategy_id, now=T0 + timedelta(hours=1))

    assert len(bets) == 1
    bet = bets[0]
    assert bet.strategy_version == strategy_id
    assert bet.execution_status == ExecutionStatus.ACCEPTED
    assert bet.requested_odds == 2.30
    assert bet.accepted_odds == 2.30
    assert bet.accepted_stake == 1.0
    assert bet.outcome is None  # not settled yet
    assert bet.consensus_closing_odds is None  # no closing snapshot recorded


def test_assemble_executed_bets_includes_outcome_once_settled(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    execution, bench_ev_id = _real_decided_bet(conn, strategy_id)

    canonical_id = get_or_create_canonical_event(conn, bench_ev_id, sport="soccer", now=T0 + timedelta(hours=6))
    evidence_id = record_result_evidence(conn, canonical_id, provider="espn", retrieved_at=T0 + timedelta(hours=6), status="final", home_goals=2, away_goals=1)
    record_settlement_version(
        conn, canonical_id, MarketType.MATCH_WINNER, None, Selection.HOME, evidence_id,
        home_goals=2, away_goals=1, created_at=T0 + timedelta(hours=6),
    )

    bets = assemble_executed_bets(conn, strategy_id, now=T0 + timedelta(hours=6))

    assert bets[0].outcome == SettlementResult.WON
    assert bets[0].canonical_event_id == canonical_id  # reused the same bootstrap, not a second one


def test_run_evaluation_produces_a_real_report_with_correct_headline_profit(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    _real_decided_bet(conn, strategy_id)

    report = run_evaluation(
        conn, strategy_id, code_sha="abc123", config={"threshold": 0.03},
        db_snapshot_hash="deadbeef", data_cutoff=T0 + timedelta(hours=1), now=T0 + timedelta(hours=1),
    )

    assert report["metrics"]["total_decisions"] == 1
    assert report["metrics"]["settled_bets"] == 0  # not settled - no profit contribution yet
    assert report["metrics"]["total_profit"] == 0.0

    row = conn.execute("SELECT COUNT(*) FROM evaluation_run").fetchone()
    assert row[0] == 1
