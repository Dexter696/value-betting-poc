from datetime import datetime, timedelta, timezone

from vb.models import MarketType, Selection
from vb.opportunity import LegReading, OpportunityTracker
from vb.storage import init_db, load_opportunity, save_opportunity

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
