from datetime import datetime, timedelta, timezone

from vb.episode import EpisodeTracker, LegReadingV2
from vb.identity import content_hash, new_id
from vb.models import (
    CanonicalEvent,
    CaptureRun,
    EventVersionV2,
    MarketSnapshotV2,
    MarketType,
    Outcome,
    RejectReason,
    Selection,
    SourceRun,
    StrategyDefinition,
)
from vb.storage import (
    get_or_create_strategy_definition,
    init_db,
    save_canonical_event,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_source_run,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
MARKET_IDENTITY = "canonical-1:match_winner:None:home:vs:swisslos.ch"


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _strategy_id(conn):
    config = {"threshold": 0.03}
    s = StrategyDefinition(id=new_id(), signal_model="raw-v1", threshold=0.03, max_age_s=90,
                            max_skew_s=60, min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0)
    return get_or_create_strategy_definition(conn, s)


def _snapshot(conn, at, odds=2.0):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=at, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site="pinnacle.com", mode="quick", started_at=at))
    ev_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=ev_id, site="pinnacle.com", event_id="e1", valid_from=at, source_run_id=source_run_id,
        sport="soccer", competition="Test League", kickoff_utc=at + timedelta(hours=3), home_team="A", away_team="B",
    ))
    snap_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snap_id, source_run_id=source_run_id, event_version_id=ev_id, market_type=MarketType.MATCH_WINNER,
        line=None, outcomes=(Outcome(Selection.HOME, odds), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=at,
    ))
    return snap_id


def _reading(conn, minute, edge, **kwargs):
    at = T0 + timedelta(minutes=minute)
    bench = _snapshot(conn, at, 2.0)
    comp = _snapshot(conn, at, 2.0 * (1 + edge))
    return LegReadingV2(
        received_at=at, edge=edge, benchmark_snapshot_id=bench, comparison_snapshot_id=comp,
        edge_model="raw-v1", **kwargs,
    )


def test_readings_below_threshold_never_open_an_episode(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    for m in range(3):
        result = tracker.ingest(_reading(conn, m, 0.01))
        assert result.episode_id is None

    count = conn.execute("SELECT COUNT(*) FROM signal_episode").fetchone()[0]
    assert count == 0


def test_full_lifecycle_open_then_close_on_drop(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    r1 = tracker.ingest(_reading(conn, 0, 0.04))
    assert r1.opened and r1.episode_id is not None
    episode_id = r1.episode_id

    r2 = tracker.ingest(_reading(conn, 1, 0.06))
    assert r2.episode_id == episode_id and not r2.closed

    r3 = tracker.ingest(_reading(conn, 2, 0.01))  # drops below threshold
    assert r3.episode_id == episode_id and r3.closed

    row = conn.execute("SELECT ended_at, end_reason FROM signal_episode WHERE id = ?", (episode_id,)).fetchone()
    assert row == ((T0 + timedelta(minutes=2)).isoformat(), "dropped_below_threshold")

    obs_count = conn.execute("SELECT COUNT(*) FROM signal_observation WHERE episode_id = ?", (episode_id,)).fetchone()[0]
    assert obs_count == 3  # every reading recorded, including the closing one


def test_market_suspended_closes_episode(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    tracker.ingest(_reading(conn, 0, 0.05))
    result = tracker.ingest(_reading(conn, 1, 0.05, market_suspended=True))

    assert result.closed
    reason = conn.execute("SELECT end_reason FROM signal_episode WHERE id = ?", (result.episode_id,)).fetchone()[0]
    assert reason == "market_suspended"


def test_event_started_closes_episode(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    tracker.ingest(_reading(conn, 0, 0.05))
    result = tracker.ingest(_reading(conn, 1, 0.05, event_started=True))

    assert result.closed
    reason = conn.execute("SELECT end_reason FROM signal_episode WHERE id = ?", (result.episode_id,)).fetchone()[0]
    assert reason == "event_started"


def test_ineligible_reading_recorded_but_never_opens_an_episode(tmp_path):
    # F-01: a stale/skewed pair is still an auditable observation, but
    # must never open (or extend) an episode even if its edge would
    # otherwise clear the threshold.
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    reading = _reading(conn, 0, 0.09, eligible=False, reject_reason=RejectReason.SNAPSHOT_SKEW)
    result = tracker.ingest(reading)

    assert result.episode_id is None
    assert result.observation_id is not None
    row = conn.execute("SELECT eligible, reject_reason, episode_id FROM signal_observation WHERE id = ?", (result.observation_id,)).fetchone()
    assert row == (0, "snapshot_skew", None)
    assert conn.execute("SELECT COUNT(*) FROM signal_episode").fetchone()[0] == 0


def test_repeated_ineligible_reading_on_an_open_episode_is_a_no_op_not_a_crash(tmp_path):
    # Found live 2026-07-28: if the "latest" snapshot pair for a market
    # hasn't changed since the last cycle (a real possibility under an
    # irregular capture cadence, F-07) and that pair is freshness-
    # ineligible, re-ingesting it used to unconditionally try to
    # INSERT a second signal_observation row identical to the one
    # already recorded - violating the UNIQUE(episode_id,
    # benchmark_snapshot_id, comparison_snapshot_id, edge_model)
    # constraint and crashing the whole pipeline cycle.
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    open_result = tracker.ingest(_reading(conn, 0, 0.05))
    assert open_result.opened

    stale_reading = _reading(conn, 5, 0.05, eligible=False, reject_reason=RejectReason.BENCHMARK_STALE)
    first = tracker.ingest(stale_reading)
    assert first.observation_id is not None

    # a second cycle re-evaluates the SAME (still-frozen) snapshot pair -
    # must be a silent no-op, not a crash
    second = tracker.ingest(stale_reading)
    assert second.observation_id is None
    assert second.episode_id == open_result.episode_id

    count = conn.execute(
        "SELECT COUNT(*) FROM signal_observation WHERE episode_id = ? AND eligible = 0", (open_result.episode_id,)
    ).fetchone()[0]
    assert count == 1  # not 2 - the duplicate was skipped, not inserted twice


def test_ingest_ignores_a_reading_no_newer_than_the_last_one(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    r1 = tracker.ingest(_reading(conn, 0, 0.05))
    episode_id = r1.episode_id
    obs_before = conn.execute("SELECT COUNT(*) FROM signal_observation WHERE episode_id = ?", (episode_id,)).fetchone()[0]

    # Same received_at as the first reading - simulates the pipeline
    # being re-run against data that hasn't been re-captured yet.
    stale = LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=_snapshot(conn, T0, 2.0),
        comparison_snapshot_id=_snapshot(conn, T0, 2.1), edge_model="raw-v1",
    )
    result = tracker.ingest(stale)
    obs_after = conn.execute("SELECT COUNT(*) FROM signal_observation WHERE episode_id = ?", (episode_id,)).fetchone()[0]

    assert result.observation_id is None
    assert obs_after == obs_before  # no new row was added


def test_event_started_bypasses_the_stale_timestamp_guard(tmp_path):
    # Same as v1's guard: event_started/market_suspended must get
    # through even with a frozen (non-advancing) timestamp, since
    # they're computed from wall-clock time, not from the reading's own
    # captured/received time.
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    tracker.ingest(_reading(conn, 0, 0.05))
    frozen_but_started = LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=_snapshot(conn, T0, 2.0),
        comparison_snapshot_id=_snapshot(conn, T0, 2.1), edge_model="raw-v1", event_started=True,
    )
    result = tracker.ingest(frozen_but_started)

    assert result.closed
    reason = conn.execute("SELECT end_reason FROM signal_episode WHERE id = ?", (result.episode_id,)).fetchone()[0]
    assert reason == "event_started"


def test_event_started_close_uses_kickoff_floor_not_stale_reading_time(tmp_path):
    # F-03 (2026-07-29): a close on event_started used to stamp ended_at
    # with the stale reading's own received_at, which for a match where
    # no new odds arrived since well before kickoff meant ended_at could
    # land far BEFORE the real kickoff. The precise fix: ended_at =
    # max(process_now, kickoff_at).
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    tracker.ingest(_reading(conn, 0, 0.05))  # opens at T0, stale odds frozen here
    kickoff = T0 + timedelta(hours=3)
    process_now = kickoff + timedelta(minutes=20)  # pipeline actually ran 20min after kickoff
    result = tracker.ingest(_reading(conn, 0, 0.05, event_started=True, now=process_now, kickoff_utc=kickoff))

    assert result.closed
    ended_at = conn.execute("SELECT ended_at FROM signal_episode WHERE id = ?", (result.episode_id,)).fetchone()[0]
    assert datetime.fromisoformat(ended_at) == process_now
    assert datetime.fromisoformat(ended_at) >= kickoff


def test_event_started_close_floors_ended_at_at_kickoff_even_if_process_now_is_earlier(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    tracker.ingest(_reading(conn, 0, 0.05))
    kickoff = T0 + timedelta(hours=3)
    process_now = kickoff - timedelta(minutes=1)  # event_started fired right at the kickoff boundary
    result = tracker.ingest(_reading(conn, 0, 0.05, event_started=True, now=process_now, kickoff_utc=kickoff))

    ended_at = conn.execute("SELECT ended_at FROM signal_episode WHERE id = ?", (result.episode_id,)).fetchone()[0]
    assert datetime.fromisoformat(ended_at) == kickoff  # floored up to kickoff, not process_now


def test_event_started_with_no_new_snapshot_does_not_duplicate_observation(tmp_path):
    # F-03: "must not add a new market observation if a new snapshot
    # wasn't actually fetched" - closing on stale, already-recorded odds
    # must not fabricate a second observation row at the same timestamp.
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    open_result = tracker.ingest(_reading(conn, 0, 0.05))
    obs_before = conn.execute(
        "SELECT COUNT(*) FROM signal_observation WHERE episode_id = ?", (open_result.episode_id,)
    ).fetchone()[0]

    kickoff = T0 + timedelta(hours=3)
    process_now = kickoff + timedelta(minutes=5)
    stale_at_close = _reading(conn, 0, 0.05, event_started=True, now=process_now, kickoff_utc=kickoff)
    result = tracker.ingest(stale_at_close)

    obs_after = conn.execute(
        "SELECT COUNT(*) FROM signal_observation WHERE episode_id = ?", (open_result.episode_id,)
    ).fetchone()[0]
    assert obs_after == obs_before  # no duplicate observation inserted
    assert result.observation_id is not None  # still resolves to the real prior observation (vb.closing needs this)


def test_market_suspended_close_uses_process_now_not_stale_reading_time(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    tracker.ingest(_reading(conn, 0, 0.05))
    process_now = T0 + timedelta(minutes=45)
    result = tracker.ingest(_reading(conn, 0, 0.05, market_suspended=True, now=process_now))

    ended_at = conn.execute("SELECT ended_at FROM signal_episode WHERE id = ?", (result.episode_id,)).fetchone()[0]
    assert datetime.fromisoformat(ended_at) == process_now


def test_f02_reproduction_through_the_real_tracker_class(tmp_path):
    """The audit's exact F-02 scenario, but end-to-end through
    EpisodeTracker itself rather than raw storage calls - two
    completely separate EpisodeTracker instances (standing in for two
    separate processes, since the class carries no state that could
    leak between them) must never collide or overwrite each other."""
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)

    # "process A"
    tracker_a = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)
    open_result = tracker_a.ingest(_reading(conn, 0, 0.05))
    close_result = tracker_a.ingest(_reading(conn, 5, 0.01))
    episode_a_id = open_result.episode_id
    assert close_result.closed

    # "process B" - a BRAND NEW tracker instance, sharing nothing but
    # the same open connection (standing in for a fresh process
    # against the same database)
    tracker_b = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)
    reopen_result = tracker_b.ingest(_reading(conn, 20, 0.06))
    episode_b_id = reopen_result.episode_id

    assert episode_a_id != episode_b_id
    assert reopen_result.opened

    # episode A completely intact
    row_a = conn.execute("SELECT started_at, ended_at, end_reason FROM signal_episode WHERE id = ?", (episode_a_id,)).fetchone()
    assert row_a == (T0.isoformat(), (T0 + timedelta(minutes=5)).isoformat(), "dropped_below_threshold")
    obs_a = conn.execute("SELECT COUNT(*) FROM signal_observation WHERE episode_id = ?", (episode_a_id,)).fetchone()[0]
    assert obs_a == 2

    total = conn.execute("SELECT COUNT(*) FROM signal_episode WHERE market_identity_id = ?", (MARKET_IDENTITY,)).fetchone()[0]
    assert total == 2
