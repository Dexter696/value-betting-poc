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
    RawEvent,
    RunStatus,
    Selection,
    SourceRun,
    StrategyDefinition,
)
from vb.pipeline import market_key
from vb.settlement import SettlementResult
from vb.settlement_evidence import (
    archive_raw_response,
    get_or_create_canonical_event,
    record_result_evidence,
    record_settlement_for_event,
    record_settlement_version,
    settlement_key,
)
from vb.storage import (
    current_settlement_version,
    finish_source_run,
    get_or_create_strategy_definition,
    init_db,
    save_canonical_event,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_source_run,
)

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _event(conn):
    event_id = new_id()
    save_canonical_event(conn, CanonicalEvent(id=event_id, sport="soccer", created_at=T0))
    return event_id


def _event_version(conn):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=T0, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site="pinnacle.com", mode="quick", started_at=T0))
    finish_source_run(conn, source_run_id, RunStatus.SUCCESS, T0, event_count=1, snapshot_count=1)
    event_version_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=event_version_id, site="pinnacle.com", event_id="p1", valid_from=T0, source_run_id=source_run_id,
        sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
        home_team="Liverpool", away_team="Everton",
    ))
    return event_version_id


def test_archive_raw_response_is_deterministic_and_content_addressed():
    payload = b'{"home": 2, "away": 1}'
    assert archive_raw_response(payload) == archive_raw_response(payload)
    assert archive_raw_response(payload) != archive_raw_response(b'{"home": 1, "away": 1}')


def test_settlement_key_is_stable_for_the_same_leg():
    key1 = settlement_key("event-1", MarketType.MATCH_WINNER, None, Selection.HOME)
    key2 = settlement_key("event-1", MarketType.MATCH_WINNER, None, Selection.HOME)
    assert key1 == key2


def test_record_result_evidence_and_settlement_version_round_trip(tmp_path):
    conn = _db(tmp_path)
    event_id = _event(conn)

    evidence_id = record_result_evidence(
        conn, event_id, provider="espn", retrieved_at=T0, status="final",
        home_goals=2, away_goals=1, raw_payload_hash=archive_raw_response(b"raw espn response"),
    )
    version_id = record_settlement_version(
        conn, event_id, MarketType.MATCH_WINNER, None, Selection.HOME, evidence_id,
        home_goals=2, away_goals=1, created_at=T0,
    )

    current = current_settlement_version(conn, settlement_key(event_id, MarketType.MATCH_WINNER, None, Selection.HOME))
    assert current.id == version_id
    assert current.result == SettlementResult.WON.value
    assert current.supersedes_id is None


def test_a_correction_creates_a_new_version_that_supersedes_the_old_one_without_deleting_it(tmp_path):
    conn = _db(tmp_path)
    event_id = _event(conn)
    key = settlement_key(event_id, MarketType.MATCH_WINNER, None, Selection.HOME)

    wrong_evidence = record_result_evidence(conn, event_id, provider="espn", retrieved_at=T0, status="final", home_goals=1, away_goals=1)
    first_version = record_settlement_version(
        conn, event_id, MarketType.MATCH_WINNER, None, Selection.HOME, wrong_evidence, home_goals=1, away_goals=1, created_at=T0,
    )

    corrected_evidence = record_result_evidence(
        conn, event_id, provider="manual", retrieved_at=T0, status="final", home_goals=2, away_goals=1,
        source_url="https://example.com/final-score", reviewer="mirek",
    )
    second_version = record_settlement_version(
        conn, event_id, MarketType.MATCH_WINNER, None, Selection.HOME, corrected_evidence,
        home_goals=2, away_goals=1, created_at=T0,
    )

    # the old row is still there, unedited
    old_row = conn.execute("SELECT result FROM settlement_version WHERE id = ?", (first_version,)).fetchone()
    assert old_row[0] == SettlementResult.LOST.value  # 1-1 draw, HOME selection lost

    current = current_settlement_version(conn, key)
    assert current.id == second_version
    assert current.result == SettlementResult.WON.value  # corrected to 2-1 home win
    assert current.supersedes_id == first_version

    total_versions = conn.execute("SELECT COUNT(*) FROM settlement_version WHERE settlement_key = ?", (key,)).fetchone()[0]
    assert total_versions == 2


def test_settle_raising_on_an_invalid_selection_propagates_from_record_settlement_version(tmp_path):
    import pytest
    conn = _db(tmp_path)
    event_id = _event(conn)
    evidence_id = record_result_evidence(conn, event_id, provider="espn", retrieved_at=T0, status="final", home_goals=2, away_goals=1)

    with pytest.raises(ValueError):
        record_settlement_version(
            conn, event_id, MarketType.TOTALS, 2.5, Selection.HOME, evidence_id,  # HOME is invalid for totals
            home_goals=2, away_goals=1, created_at=T0,
        )


def test_get_or_create_canonical_event_creates_a_new_one_for_a_fresh_event_version(tmp_path):
    conn = _db(tmp_path)
    event_version_id = _event_version(conn)

    canonical_id = get_or_create_canonical_event(conn, event_version_id, sport="soccer", now=T0)

    assert conn.execute("SELECT COUNT(*) FROM canonical_event WHERE id = ?", (canonical_id,)).fetchone()[0] == 1
    row = conn.execute(
        "SELECT role, orientation, tier, model_version FROM event_match WHERE event_version_id = ?", (event_version_id,)
    ).fetchone()
    assert row == ("benchmark", "same", "auto", "bootstrap-v1")


def test_get_or_create_canonical_event_reuses_the_existing_link_on_a_second_call(tmp_path):
    conn = _db(tmp_path)
    event_version_id = _event_version(conn)

    first_id = get_or_create_canonical_event(conn, event_version_id, sport="soccer", now=T0)
    second_id = get_or_create_canonical_event(conn, event_version_id, sport="soccer", now=T0 + timedelta(minutes=5))

    assert first_id == second_id
    assert conn.execute("SELECT COUNT(*) FROM canonical_event").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_match").fetchone()[0] == 1


def test_get_or_create_canonical_event_gives_different_event_versions_different_canonical_ids(tmp_path):
    conn = _db(tmp_path)
    a = _event_version(conn)
    b = _event_version(conn)

    id_a = get_or_create_canonical_event(conn, a, sport="soccer", now=T0)
    id_b = get_or_create_canonical_event(conn, b, sport="soccer", now=T0)

    assert id_a != id_b


def _real_snapshot(conn, at, odds, site="pinnacle.com"):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=at, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site=site, mode="quick", started_at=at))
    finish_source_run(conn, source_run_id, RunStatus.SUCCESS, at, event_count=1, snapshot_count=1)
    event_version_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=event_version_id, site=site, event_id="p1", valid_from=at, source_run_id=source_run_id,
        sport="soccer", competition="Premier League", kickoff_utc=at + timedelta(hours=5),
        home_team="Liverpool", away_team="Everton",
    ))
    snap_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snap_id, source_run_id=source_run_id, event_version_id=event_version_id, market_type=MarketType.MATCH_WINNER,
        line=None, outcomes=(Outcome(Selection.HOME, odds), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=at,
    ))
    return snap_id, event_version_id


def test_record_settlement_for_event_settles_every_leg_ever_tracked_for_that_event(tmp_path):
    conn = _db(tmp_path)
    benchmark_event = RawEvent(
        site="pinnacle.com", sport="soccer", competition="Premier League", kickoff_utc=T0 + timedelta(hours=5),
        raw_home_team="Liverpool", raw_away_team="Everton", event_id="p1",
    )
    market_identity = market_key(benchmark_event, MarketType.MATCH_WINNER, None, Selection.HOME, "swisslos.ch")

    config = {"threshold": 0.03}
    strategy_id = get_or_create_strategy_definition(conn, StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=0.03, max_age_s=90, max_skew_s=60,
        min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0,
    ))

    bench_snap, _ = _real_snapshot(conn, T0, 2.0, site="pinnacle.com")
    comp_snap, _ = _real_snapshot(conn, T0, 2.30, site="swisslos.ch")
    tracker = EpisodeTracker(conn, strategy_id, market_identity, threshold=0.03)
    tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))
    assert conn.execute("SELECT COUNT(*) FROM signal_episode").fetchone()[0] == 1

    legs_settled = record_settlement_for_event(
        conn, "pinnacle.com", "p1", provider="espn", home_goals=2, away_goals=1, now=T0 + timedelta(hours=6),
    )

    assert legs_settled == 1
    key = settlement_key(
        conn.execute("SELECT canonical_event_id FROM event_match").fetchone()[0],
        MarketType.MATCH_WINNER, None, Selection.HOME,
    )
    current = current_settlement_version(conn, key)
    assert current is not None
    assert current.result == SettlementResult.WON.value  # Liverpool (home) won 2-1


def test_record_settlement_for_event_returns_zero_when_the_event_was_never_captured_into_v2(tmp_path):
    conn = _db(tmp_path)
    legs_settled = record_settlement_for_event(
        conn, "pinnacle.com", "never-seen", provider="espn", home_goals=1, away_goals=0, now=T0,
    )
    assert legs_settled == 0
    assert conn.execute("SELECT COUNT(*) FROM result_evidence").fetchone()[0] == 0
