from datetime import datetime, timedelta, timezone

from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from vb.opportunity import LegReading, OpportunityTracker
from vb.reporting import build_finished_matches_report, pre_entry_history_for_opportunity
from vb.settlement import SettlementResult
from vb.storage import init_db, record_match_result, save_opportunity, save_raw_capture

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def _moneyline(event, home_odds, draw_odds, away_odds, captured_at):
    return MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, home_odds), Outcome(Selection.DRAW, draw_odds), Outcome(Selection.AWAY, away_odds)),
        captured_at=captured_at,
    )


def _event(site, event_id, home="Liverpool", away="Everton"):
    return RawEvent(
        site=site, sport="soccer", competition="Premier League",
        kickoff_utc=T0, raw_home_team=home, raw_away_team=away, event_id=event_id,
    )


def test_report_includes_all_three_books_when_all_captured(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    pinnacle_event = _event("pinnacle.com", "p1")
    loro_event = _event("loro.ch", "l1")
    swisslos_event = _event("swisslos.ch", "s1")

    # Raw captures so _find_matching_event_id has something to match
    # Swisslos against (Loro is already the tracked comparison site, its
    # odds come from the opportunity itself, not a raw-capture lookup).
    save_raw_capture(conn, swisslos_event, [
        MarketSnapshot(
            event=swisslos_event, market_type=MarketType.MATCH_WINNER, line=None,
            outcomes=(Outcome(Selection.HOME, 2.00), Outcome(Selection.DRAW, 3.30), Outcome(Selection.AWAY, 3.90)),
            captured_at=T0,
        )
    ])

    tracker = OpportunityTracker(
        market_key="pinnacle.com:p1:match_winner:None:home:vs:loro.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="loro.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.HOME,
    )
    tracker.ingest(LegReading(captured_at=T0, edge_a=0.10, edge_b=0.08, benchmark_odds=1.90, comparison_odds=2.10))
    tracker.ingest(LegReading(captured_at=T0 + timedelta(minutes=30), edge_a=0.15, edge_b=0.12, benchmark_odds=1.90, comparison_odds=2.20))
    save_opportunity(conn, tracker.current)

    # pinnacle's own raw_event row is needed for the report to describe the match
    conn.execute(
        "INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pinnacle_event.site, pinnacle_event.event_id, pinnacle_event.sport, pinnacle_event.competition,
         pinnacle_event.kickoff_utc.isoformat(), pinnacle_event.raw_home_team, pinnacle_event.raw_away_team),
    )
    conn.commit()

    record_match_result(conn, "pinnacle.com", "p1", home_goals=2, away_goals=0, source="test")

    reports = build_finished_matches_report(conn)

    assert len(reports) == 1
    report = reports[0]
    assert report.home_team == "Liverpool" and report.away_team == "Everton"
    assert report.home_goals == 2 and report.away_goals == 0

    assert len(report.legs) == 1
    leg = report.legs[0]
    assert leg.entry_edge_a == 0.10
    assert round(leg.peak_edge_a, 10) == 0.15
    assert leg.outcome == SettlementResult.WON  # home won 2-0

    odds_by_site = {b.site: b.odds for b in leg.book_odds}
    assert odds_by_site["pinnacle.com"] == 1.90  # benchmark, from opportunity entry
    assert odds_by_site["loro.ch"] == 2.10        # tracked comparison site, from opportunity entry
    assert odds_by_site["swisslos.ch"] == 2.00    # untracked site, found via matching + raw capture


def test_report_shows_none_for_a_site_that_never_captured_the_match(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    pinnacle_event = _event("pinnacle.com", "p1")
    conn.execute(
        "INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pinnacle_event.site, pinnacle_event.event_id, pinnacle_event.sport, pinnacle_event.competition,
         pinnacle_event.kickoff_utc.isoformat(), pinnacle_event.raw_home_team, pinnacle_event.raw_away_team),
    )
    conn.commit()

    tracker = OpportunityTracker(
        market_key="pinnacle.com:p1:match_winner:None:home:vs:loro.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="loro.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.HOME,
    )
    tracker.ingest(LegReading(captured_at=T0, edge_a=0.10, edge_b=0.08, benchmark_odds=1.90, comparison_odds=2.10))
    save_opportunity(conn, tracker.current)

    record_match_result(conn, "pinnacle.com", "p1", home_goals=1, away_goals=1, source="test")

    report = build_finished_matches_report(conn)[0]
    leg = report.legs[0]
    odds_by_site = {b.site: b.odds for b in leg.book_odds}
    assert odds_by_site["swisslos.ch"] is None  # never captured this match anywhere
    assert leg.outcome == SettlementResult.LOST  # draw, home selection loses


def test_report_flags_still_open_opportunity_on_a_settled_match(tmp_path):
    # Mirrors the real gap found live: a match can be settled while its
    # opportunity is still marked open, because the tracker only closes
    # on a NEW reading and the scraper stopped seeing the match once it
    # kicked off.
    conn = init_db(tmp_path / "vb.sqlite")
    pinnacle_event = _event("pinnacle.com", "p1")
    conn.execute(
        "INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pinnacle_event.site, pinnacle_event.event_id, pinnacle_event.sport, pinnacle_event.competition,
         pinnacle_event.kickoff_utc.isoformat(), pinnacle_event.raw_home_team, pinnacle_event.raw_away_team),
    )
    conn.commit()

    tracker = OpportunityTracker(
        market_key="pinnacle.com:p1:match_winner:None:draw:vs:loro.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="loro.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.DRAW,
    )
    tracker.ingest(LegReading(captured_at=T0, edge_a=0.09, edge_b=0.07, benchmark_odds=3.64, comparison_odds=4.05))
    save_opportunity(conn, tracker.current)

    record_match_result(conn, "pinnacle.com", "p1", home_goals=0, away_goals=0, source="test")

    leg = build_finished_matches_report(conn)[0].legs[0]
    assert leg.is_open is True
    assert leg.convergence is None
    assert leg.outcome == SettlementResult.WON  # draw happened


def test_pre_entry_history_reconstructs_readings_before_threshold_crossed(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    pinnacle_event = _event("pinnacle.com", "p1")
    loro_event = _event("loro.ch", "l1")

    # Three below-threshold cycles never tracked as an opportunity - only
    # raw_market_snapshot has them, since that's captured every cycle
    # regardless of edge.
    for i, cmp_odds in enumerate([2.02, 2.04, 2.05]):
        t = T0 + timedelta(minutes=i * 5)
        save_raw_capture(conn, pinnacle_event, [_moneyline(pinnacle_event, 2.00, 3.30, 3.90, captured_at=t)])
        save_raw_capture(conn, loro_event, [_moneyline(loro_event, cmp_odds, 3.30, 3.90, captured_at=t)])

    entry_time = T0 + timedelta(minutes=15)
    save_raw_capture(conn, pinnacle_event, [_moneyline(pinnacle_event, 2.00, 3.30, 3.90, captured_at=entry_time)])
    save_raw_capture(conn, loro_event, [_moneyline(loro_event, 2.10, 3.30, 3.90, captured_at=entry_time)])

    tracker = OpportunityTracker(
        market_key="pinnacle.com:p1:match_winner:None:home:vs:loro.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="loro.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.HOME,
    )
    tracker.ingest(LegReading(captured_at=entry_time, edge_a=0.05, edge_b=0.04, benchmark_odds=2.00, comparison_odds=2.10))
    opp = tracker.current

    history = pre_entry_history_for_opportunity(conn, opp)

    assert len(history) == 3
    assert [h.comparison_odds for h in history] == [2.02, 2.04, 2.05]  # oldest first
    assert all(h.benchmark_odds == 2.00 for h in history)
    assert all(h.captured_at < opp.first_cross_at for h in history)
    assert all(h.edge_a < 0.03 for h in history)  # genuinely below threshold, that's why they never opened anything


def test_pre_entry_history_respects_limit(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    pinnacle_event = _event("pinnacle.com", "p1")
    loro_event = _event("loro.ch", "l1")

    for i in range(8):
        t = T0 + timedelta(minutes=i * 5)
        save_raw_capture(conn, pinnacle_event, [_moneyline(pinnacle_event, 2.00, 3.30, 3.90, captured_at=t)])
        save_raw_capture(conn, loro_event, [_moneyline(loro_event, 2.02, 3.30, 3.90, captured_at=t)])

    entry_time = T0 + timedelta(minutes=45)
    save_raw_capture(conn, pinnacle_event, [_moneyline(pinnacle_event, 2.00, 3.30, 3.90, captured_at=entry_time)])
    save_raw_capture(conn, loro_event, [_moneyline(loro_event, 2.10, 3.30, 3.90, captured_at=entry_time)])

    tracker = OpportunityTracker(
        market_key="pinnacle.com:p1:match_winner:None:home:vs:loro.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="loro.ch",
        market_type=MarketType.MATCH_WINNER, line=None, selection=Selection.HOME,
    )
    tracker.ingest(LegReading(captured_at=entry_time, edge_a=0.05, edge_b=0.04, benchmark_odds=2.00, comparison_odds=2.10))

    history = pre_entry_history_for_opportunity(conn, tracker.current, limit=5)

    assert len(history) == 5  # 8 available, capped at limit
