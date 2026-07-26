from datetime import datetime, timedelta, timezone

from vb.identity import content_hash, new_id
from vb.models import (
    BetDecision,
    BetDecisionChoice,
    CanonicalEvent,
    CaptureRun,
    EpisodeEndReason,
    EventMatchV2,
    EventVersionV2,
    MarketType,
    MatchOrientation,
    MatchRole,
    MatchTierV2,
    Outcome,
    RunStatus,
    Selection,
    SignalEpisode,
    SignalObservation,
    StrategyDefinition,
)
from vb.storage import (
    close_signal_episode,
    find_open_signal_episode,
    get_or_create_strategy_definition,
    init_db,
    open_signal_episode,
    save_bet_decision,
    save_canonical_event,
    save_capture_run,
    save_event_match_v2,
    save_event_version,
    save_market_snapshot_v2,
    save_signal_observation,
    save_source_run,
)
from vb.models import MarketSnapshotV2, SourceRun

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _strategy(conn):
    config = {"signal_model": "raw-v1", "threshold": 0.03, "max_age_s": 90, "max_skew_s": 60, "min_lead_time_s": 300}
    strategy = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=0.03, max_age_s=90, max_skew_s=60,
        min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0,
    )
    return get_or_create_strategy_definition(conn, strategy)


def _real_snapshot(conn, captured_at=T0, odds=2.0):
    """Insert a real, minimal but FK-valid market_snapshot_v2 row (via
    a real capture_run/source_run/event_version chain) and return its
    id - signal_observation's FKs point at real snapshots, on purpose,
    so tests exercise the same referential integrity production code
    must satisfy rather than bypassing it with fake string ids."""
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=captured_at, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site="pinnacle.com", mode="quick", started_at=captured_at))
    event_version_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=event_version_id, site="pinnacle.com", event_id="e1", valid_from=captured_at, source_run_id=source_run_id,
        sport="soccer", competition="Test League", kickoff_utc=captured_at + timedelta(hours=3),
        home_team="Home", away_team="Away",
    ))
    snapshot_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snapshot_id, source_run_id=source_run_id, event_version_id=event_version_id,
        market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, odds), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=captured_at,
    ))
    return snapshot_id


def test_get_or_create_strategy_definition_is_content_addressed(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    config = {"a": 1}
    s1 = StrategyDefinition(id=new_id(), signal_model="m", threshold=0.03, max_age_s=1, max_skew_s=1,
                             min_lead_time_s=1, config=config, config_hash=content_hash(config), created_at=T0)
    s2 = StrategyDefinition(id=new_id(), signal_model="m", threshold=0.03, max_age_s=1, max_skew_s=1,
                             min_lead_time_s=1, config=config, config_hash=content_hash(config), created_at=T0)

    id1 = get_or_create_strategy_definition(conn, s1)
    id2 = get_or_create_strategy_definition(conn, s2)

    assert id1 == id2  # identical config resolves to the SAME row, not a duplicate
    count = conn.execute("SELECT COUNT(*) FROM strategy_definition").fetchone()[0]
    assert count == 1


def test_f02_restart_recross_does_not_collide_or_overwrite(tmp_path):
    """Direct reproduction of the audit's F-02 finding, against the new
    schema-v2 mechanism instead of v1's OpportunityTracker.

    v1's bug: OpportunityTracker._instance_seq always started at 0 in a
    fresh process; a re-crossing after a close, handled by a NEW
    process with no memory of the prior one, re-minted the same
    instance_id ("...#1") an earlier, unrelated opportunity had already
    used. save_opportunity()'s upsert then silently overwrote that
    earlier row's header and deleted-and-rewrote its entire snapshot
    trajectory.

    This test simulates the same two-separate-processes scenario -
    "process A" opens and closes an episode, "process B" (a completely
    independent call with no shared state, standing in for a fresh
    process) then handles a genuine re-crossing of the SAME market -
    and asserts process A's episode is completely untouched afterward.
    """
    conn = init_db(tmp_path / "vb.sqlite")
    strategy_id = _strategy(conn)
    market_identity_id = "canonical-event-123:match_winner:None:home:vs:swisslos.ch"

    # --- "process A": opens an episode, records an observation, closes it ---
    episode_a_id = new_id()
    episode_a = SignalEpisode(id=episode_a_id, strategy_version=strategy_id, market_identity_id=market_identity_id, started_at=T0)
    open_signal_episode(conn, episode_a)

    bench_a, comp_a = _real_snapshot(conn, T0, 2.0), _real_snapshot(conn, T0, 2.1)
    obs_a_id = new_id()
    save_signal_observation(conn, SignalObservation(
        id=obs_a_id, decision_time=T0, benchmark_snapshot_id=bench_a, comparison_snapshot_id=comp_a,
        edge_model="raw-v1", edge=0.05, eligible=True, episode_id=episode_a_id,
    ))

    close_signal_episode(conn, episode_a_id, T0 + timedelta(minutes=10), EpisodeEndReason.DROPPED_BELOW_THRESHOLD)

    # --- "process B": a fresh call, no shared state with the above, handles
    # a genuine re-crossing of the exact same market_identity_id+strategy ---
    assert find_open_signal_episode(conn, strategy_id, market_identity_id) is None  # correctly sees nothing open

    episode_b_id = new_id()
    episode_b = SignalEpisode(
        id=episode_b_id, strategy_version=strategy_id, market_identity_id=market_identity_id,
        started_at=T0 + timedelta(minutes=20),
    )
    open_signal_episode(conn, episode_b)

    bench_b = _real_snapshot(conn, T0 + timedelta(minutes=20), 2.05)
    comp_b = _real_snapshot(conn, T0 + timedelta(minutes=20), 2.15)
    obs_b_id = new_id()
    save_signal_observation(conn, SignalObservation(
        id=obs_b_id, decision_time=T0 + timedelta(minutes=20), benchmark_snapshot_id=bench_b,
        comparison_snapshot_id=comp_b, edge_model="raw-v1", edge=0.04, eligible=True, episode_id=episode_b_id,
    ))

    # --- the actual F-02 assertion: two genuinely different identities,
    # and episode A's data is completely intact ---
    assert episode_a_id != episode_b_id

    row_a = conn.execute(
        "SELECT started_at, ended_at, end_reason FROM signal_episode WHERE id = ?", (episode_a_id,)
    ).fetchone()
    assert row_a == (T0.isoformat(), (T0 + timedelta(minutes=10)).isoformat(), "dropped_below_threshold")

    obs_a_count = conn.execute(
        "SELECT COUNT(*) FROM signal_observation WHERE episode_id = ?", (episode_a_id,)
    ).fetchone()[0]
    assert obs_a_count == 1  # process B's observation did not land under episode A

    row_b = conn.execute(
        "SELECT started_at, ended_at FROM signal_episode WHERE id = ?", (episode_b_id,)
    ).fetchone()
    assert row_b == ((T0 + timedelta(minutes=20)).isoformat(), None)  # B is genuinely still open

    total_episodes = conn.execute(
        "SELECT COUNT(*) FROM signal_episode WHERE market_identity_id = ?", (market_identity_id,)
    ).fetchone()[0]
    assert total_episodes == 2  # both survive as distinct rows - nothing was overwritten


def test_close_signal_episode_refuses_to_overwrite_an_already_closed_episode(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    strategy_id = _strategy(conn)
    episode_id = new_id()
    open_signal_episode(conn, SignalEpisode(id=episode_id, strategy_version=strategy_id, market_identity_id="m1", started_at=T0))
    close_signal_episode(conn, episode_id, T0 + timedelta(minutes=5), EpisodeEndReason.EVENT_STARTED)

    try:
        close_signal_episode(conn, episode_id, T0 + timedelta(minutes=99), EpisodeEndReason.MARKET_SUSPENDED)
        assert False, "expected ValueError on double-close"
    except ValueError:
        pass

    # original close is untouched
    row = conn.execute("SELECT ended_at, end_reason FROM signal_episode WHERE id = ?", (episode_id,)).fetchone()
    assert row == ((T0 + timedelta(minutes=5)).isoformat(), "event_started")


def test_signal_observation_records_rejected_pairs_not_just_eligible_ones(tmp_path):
    # F-01: a stale/skewed pair must still produce an auditable row.
    conn = init_db(tmp_path / "vb.sqlite")
    from vb.models import RejectReason

    b1, c1 = _real_snapshot(conn, T0, 2.0), _real_snapshot(conn, T0 - timedelta(hours=20), 2.1)
    obs_id = new_id()
    save_signal_observation(conn, SignalObservation(
        id=obs_id, decision_time=T0, benchmark_snapshot_id=b1, comparison_snapshot_id=c1,
        edge_model="raw-v1", edge=0.09, eligible=False, episode_id=None, reject_reason=RejectReason.SNAPSHOT_SKEW,
    ))
    row = conn.execute("SELECT eligible, reject_reason, episode_id FROM signal_observation WHERE id = ?", (obs_id,)).fetchone()
    assert row == (0, "snapshot_skew", None)


def test_bet_decision_idempotency_key_is_unique(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    strategy_id = _strategy(conn)
    b1, c1 = _real_snapshot(conn, T0, 2.0), _real_snapshot(conn, T0, 2.1)
    obs_id = new_id()
    save_signal_observation(conn, SignalObservation(
        id=obs_id, decision_time=T0, benchmark_snapshot_id=b1, comparison_snapshot_id=c1,
        edge_model="raw-v1", edge=0.05, eligible=True, episode_id=None,
    ))
    decision = BetDecision(
        id=new_id(), strategy_version=strategy_id, signal_observation_id=obs_id, decided_at=T0,
        decision=BetDecisionChoice.BET, reason="edge above threshold", idempotency_key="unique-key-1",
    )
    save_bet_decision(conn, decision)

    duplicate = BetDecision(
        id=new_id(), strategy_version=strategy_id, signal_observation_id=obs_id, decided_at=T0,
        decision=BetDecisionChoice.BET, reason="edge above threshold", idempotency_key="unique-key-1",
    )
    try:
        save_bet_decision(conn, duplicate)
        assert False, "expected sqlite3.IntegrityError on duplicate idempotency_key"
    except Exception as e:
        assert "UNIQUE" in str(e) or "unique" in str(e).lower()


def test_full_provenance_chain_round_trip(tmp_path):
    """capture_run -> source_run -> event_version -> canonical_event ->
    event_match -> market_snapshot_v2, all linked and readable back -
    proves the chain the audit's F-06/F-15 wanted actually works end to
    end, not just that each table exists in isolation."""
    conn = init_db(tmp_path / "vb.sqlite")

    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=T0, git_sha="abc123", schema_version=2))

    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site="pinnacle.com", mode="quick", started_at=T0))

    event_version_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=event_version_id, site="pinnacle.com", event_id="p1", valid_from=T0, source_run_id=source_run_id,
        sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=3),
        home_team="Liverpool", away_team="Everton",
    ))

    canonical_id = new_id()
    save_canonical_event(conn, CanonicalEvent(id=canonical_id, sport="soccer", created_at=T0))

    save_event_match_v2(conn, EventMatchV2(
        id=new_id(), canonical_event_id=canonical_id, event_version_id=event_version_id,
        role=MatchRole.BENCHMARK, orientation=MatchOrientation.SAME, score=0.95,
        score_components={"team_score": 1.0, "time_score": 1.0}, model_version="bipartite-v1",
        tier=MatchTierV2.AUTO, decided_at=T0,
    ))

    snapshot_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snapshot_id, source_run_id=source_run_id, event_version_id=event_version_id,
        market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, 2.1), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=T0,
    ))

    # walk the chain back
    joined = conn.execute(
        """
        SELECT cr.git_sha, sr.site, ev.competition, ce.sport, em.orientation, ms.market_type
        FROM market_snapshot_v2 ms
        JOIN event_version ev ON ev.id = ms.event_version_id
        JOIN source_run sr ON sr.id = ms.source_run_id
        JOIN capture_run cr ON cr.id = sr.capture_run_id
        JOIN event_match em ON em.event_version_id = ev.id
        JOIN canonical_event ce ON ce.id = em.canonical_event_id
        WHERE ms.id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    assert joined == ("abc123", "pinnacle.com", "Premier League", "soccer", "same", "match_winner")
