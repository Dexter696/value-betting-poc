from datetime import datetime, timedelta, timezone

from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from vb.opportunity import OpportunityTracker
from vb.settlement import SettlementResult
from vb.storage import (
    init_db,
    load_settlement,
    record_match_result,
    save_opportunity,
    save_settlement,
)

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def test_save_and_load_settlement(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    save_settlement(
        conn, "pinnacle.com", "evt1", MarketType.MATCH_WINNER, None, Selection.HOME,
        SettlementResult.WON, home_goals=2, away_goals=1, source="manual",
    )

    result = load_settlement(conn, "pinnacle.com", "evt1", MarketType.MATCH_WINNER, None, Selection.HOME)
    assert result == SettlementResult.WON


def test_load_settlement_missing_returns_none(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    assert load_settlement(conn, "pinnacle.com", "evt1", MarketType.MATCH_WINNER, None, Selection.HOME) is None


def test_resave_updates_outcome(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    save_settlement(conn, "pinnacle.com", "evt1", MarketType.MATCH_WINNER, None, Selection.HOME, SettlementResult.WON)
    save_settlement(conn, "pinnacle.com", "evt1", MarketType.MATCH_WINNER, None, Selection.HOME, SettlementResult.LOST)

    assert load_settlement(conn, "pinnacle.com", "evt1", MarketType.MATCH_WINNER, None, Selection.HOME) == SettlementResult.LOST


def test_record_match_result_settles_every_tracked_leg(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    # Simulate what run_cycle would have produced: two opportunities for
    # the same benchmark event, different markets, possibly different
    # comparison sites - both should get settled from one final score.
    tracker_a = OpportunityTracker(
        market_key="pinnacle.com:evt1:match_winner:None:home:vs:swisslos.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="swisslos.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.HOME,
    )
    from vb.opportunity import LegReading
    tracker_a.ingest(LegReading(captured_at=T0, edge_a=0.05, edge_b=0.04, benchmark_odds=2.0, comparison_odds=2.1))
    save_opportunity(conn, tracker_a.current)

    tracker_b = OpportunityTracker(
        market_key="pinnacle.com:evt1:totals:2.5:over:vs:loro.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="loro.ch",
        market_type=MarketType.TOTALS, line=2.5, selection=Selection.OVER,
    )
    tracker_b.ingest(LegReading(captured_at=T0, edge_a=0.04, edge_b=0.03, benchmark_odds=1.9, comparison_odds=2.0))
    save_opportunity(conn, tracker_b.current)

    # An unrelated event's opportunity must not get swept in.
    tracker_other = OpportunityTracker(
        market_key="pinnacle.com:evt2:match_winner:None:home:vs:swisslos.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="swisslos.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.HOME,
    )
    tracker_other.ingest(LegReading(captured_at=T0, edge_a=0.05, edge_b=0.04, benchmark_odds=2.0, comparison_odds=2.1))
    save_opportunity(conn, tracker_other.current)

    settled_count = record_match_result(conn, "pinnacle.com", "evt1", home_goals=2, away_goals=1, source="manual")

    assert settled_count == 2
    assert load_settlement(conn, "pinnacle.com", "evt1", MarketType.MATCH_WINNER, None, Selection.HOME) == SettlementResult.WON
    assert load_settlement(conn, "pinnacle.com", "evt1", MarketType.TOTALS, 2.5, Selection.OVER) == SettlementResult.WON
    # unrelated event untouched
    assert load_settlement(conn, "pinnacle.com", "evt2", MarketType.MATCH_WINNER, None, Selection.HOME) is None
