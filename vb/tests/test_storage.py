from datetime import datetime, timedelta, timezone

from vb.models import MarketType, RawEvent, Selection
from vb.opportunity import LegReading, OpportunityTracker
from vb.storage import (
    force_resolve_stale_opportunities,
    init_db,
    load_opportunity,
    save_opportunity,
    save_raw_capture,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _build_closed_opportunity():
    tracker = OpportunityTracker(
        market_key="pinnacle.com:evt42",
        sport="soccer",
        benchmark_site="pinnacle.com",
        comparison_site="swisslos.ch",
        market_type=MarketType.ASIAN_HANDICAP,
        line=-1.5,
        selection=Selection.HOME,
    )
    tracker.ingest(
        LegReading(
            captured_at=T0, edge_a=0.04, edge_b=0.03, benchmark_odds=1.90, comparison_odds=2.00,
            max_bet_size=500.0, full_market={"pinnacle.com": {"home": 1.90, "away": 1.95}},
        )
    )
    tracker.ingest(
        LegReading(
            captured_at=T0 + timedelta(minutes=1), edge_a=0.01, edge_b=0.005,
            benchmark_odds=1.90, comparison_odds=1.92, max_bet_size=500.0,
            full_market={"pinnacle.com": {"home": 1.90, "away": 1.95}},
        )
    )
    return tracker.completed[0]


def test_init_db_creates_missing_parent_directory(tmp_path):
    # Matters for a fresh checkout (e.g. GitHub Actions) where data/ isn't
    # tracked in git at all - sqlite3.connect() alone can't create it.
    nested_path = tmp_path / "does" / "not" / "exist" / "vb.sqlite"
    conn = init_db(nested_path)
    assert nested_path.exists()
    conn.execute("SELECT 1")  # connection actually usable


def test_save_and_load_round_trip(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    opp = _build_closed_opportunity()

    save_opportunity(conn, opp)
    loaded = load_opportunity(conn, opp.instance_id)

    assert loaded is not None
    assert loaded.instance_id == opp.instance_id
    assert loaded.market_key == opp.market_key
    assert loaded.market_type == MarketType.ASIAN_HANDICAP
    assert loaded.line == -1.5
    assert loaded.selection == Selection.HOME
    assert loaded.resolution_reason == opp.resolution_reason
    assert loaded.first_cross_at == opp.first_cross_at
    assert loaded.resolved_at == opp.resolved_at

    assert len(loaded.snapshots) == 2
    assert [s.edge_a for s in loaded.snapshots] == [0.04, 0.01]
    assert loaded.snapshots[0].full_market == {"pinnacle.com": {"home": 1.90, "away": 1.95}}
    assert loaded.snapshots[0].max_bet_size == 500.0


def test_resave_replaces_snapshots_not_duplicates(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    opp = _build_closed_opportunity()

    save_opportunity(conn, opp)
    save_opportunity(conn, opp)  # simulate a re-write while still accumulating

    loaded = load_opportunity(conn, opp.instance_id)
    assert len(loaded.snapshots) == 2


def test_load_missing_opportunity_returns_none(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    assert load_opportunity(conn, "does-not-exist") is None


def _open_opportunity_with_kickoff(conn, event_id: str, kickoff: datetime):
    save_raw_capture(
        conn,
        RawEvent(
            site="pinnacle.com", sport="soccer", competition="Test League",
            kickoff_utc=kickoff, raw_home_team="Home", raw_away_team="Away", event_id=event_id,
        ),
        [],
    )
    tracker = OpportunityTracker(
        market_key=f"pinnacle.com:{event_id}:match_winner:None:home:vs:swisslos.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="swisslos.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.HOME,
    )
    tracker.ingest(
        LegReading(captured_at=kickoff - timedelta(hours=1), edge_a=0.05, edge_b=0.04, benchmark_odds=2.0, comparison_odds=2.1)
    )
    opp = tracker.current
    save_opportunity(conn, opp)
    return opp.instance_id


def test_force_resolve_closes_stale_open_opportunity(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    now = datetime.now(timezone.utc)
    stale_id = _open_opportunity_with_kickoff(conn, "stale-evt", now - timedelta(hours=6))

    count = force_resolve_stale_opportunities(conn, kickoff_buffer_hours=4.0)

    assert count == 1
    resolved = load_opportunity(conn, stale_id)
    assert resolved.resolved_at is not None
    assert resolved.resolution_reason.value == "event_started"
    # trajectory itself is untouched - only the header changed
    assert len(resolved.snapshots) == 1


def test_force_resolve_leaves_recent_open_opportunity_alone(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    now = datetime.now(timezone.utc)
    recent_id = _open_opportunity_with_kickoff(conn, "recent-evt", now - timedelta(hours=1))

    count = force_resolve_stale_opportunities(conn, kickoff_buffer_hours=4.0)

    assert count == 0
    still_open = load_opportunity(conn, recent_id)
    assert still_open.resolved_at is None


def test_force_resolve_leaves_already_resolved_opportunity_alone(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    opp = _build_closed_opportunity()
    save_opportunity(conn, opp)

    count = force_resolve_stale_opportunities(conn, kickoff_buffer_hours=4.0)

    assert count == 0
    reloaded = load_opportunity(conn, opp.instance_id)
    assert reloaded.resolution_reason == opp.resolution_reason  # untouched, not overwritten to event_started
