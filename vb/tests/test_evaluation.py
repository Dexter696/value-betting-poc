from datetime import datetime, timedelta, timezone

from vb.evaluation import evaluate, flat_stake_profit, odds_bucket
from vb.models import MarketType, RawEvent, Selection
from vb.opportunity import LegReading, OpportunityTracker
from vb.settlement import SettlementResult
from vb.storage import init_db, save_opportunity

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def test_odds_bucket_boundaries():
    assert odds_bucket(1.5) == "favorite"
    assert odds_bucket(1.99) == "favorite"
    assert odds_bucket(2.0) == "mid"
    assert odds_bucket(3.99) == "mid"
    assert odds_bucket(4.0) == "longshot"
    assert odds_bucket(15.0) == "longshot"


def test_flat_stake_profit_won():
    assert flat_stake_profit(SettlementResult.WON, 2.5) == 1.5


def test_flat_stake_profit_lost():
    assert flat_stake_profit(SettlementResult.LOST, 2.5) == -1.0


def test_flat_stake_profit_push():
    assert flat_stake_profit(SettlementResult.PUSH, 2.5) == 0.0


def test_flat_stake_profit_half_won():
    assert flat_stake_profit(SettlementResult.HALF_WON, 3.0) == 1.0  # 0.5 * (3-1)


def test_flat_stake_profit_half_lost():
    assert flat_stake_profit(SettlementResult.HALF_LOST, 3.0) == -0.5


def _insert_settled_leg(conn, event_id, home, away, benchmark_odds, comparison_odds, edge_a, edge_b, outcome, home_goals, away_goals):
    event = RawEvent(
        site="pinnacle.com", sport="soccer", competition="Test League",
        kickoff_utc=T0, raw_home_team=home, raw_away_team=away, event_id=event_id,
    )
    conn.execute(
        "INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event.site, event.event_id, event.sport, event.competition, event.kickoff_utc.isoformat(), home, away),
    )
    conn.commit()

    tracker = OpportunityTracker(
        market_key=f"pinnacle.com:{event_id}:match_winner:None:home:vs:loro.ch",
        sport="soccer", benchmark_site="pinnacle.com", comparison_site="loro.ch",
        market_type=MarketType.MATCH_WINNER,
        line=None, selection=Selection.HOME,
    )
    tracker.ingest(LegReading(captured_at=T0, edge_a=edge_a, edge_b=edge_b, benchmark_odds=benchmark_odds, comparison_odds=comparison_odds))
    save_opportunity(conn, tracker.current)

    # directly set the desired outcome (bypassing settle()'s score-derived
    # logic, since these tests need to control the exact outcome/edge
    # combination rather than derive it from a score)
    conn.execute(
        "INSERT INTO settlement (benchmark_site, benchmark_event_id, market_type, line, selection, outcome, home_goals, away_goals, settled_at, source) "
        "VALUES (?, ?, 'match_winner', NULL, 'home', ?, ?, ?, ?, 'test')",
        ("pinnacle.com", event_id, outcome.value, home_goals, away_goals, T0.isoformat()),
    )
    conn.commit()


def test_evaluate_buckets_and_computes_stats(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    # Favorite bucket: both methods agree, wins
    _insert_settled_leg(conn, "e1", "A", "B", 1.8, 1.9, 0.05, 0.04, SettlementResult.WON, 2, 0)
    # Favorite bucket: Method A flags, Method B disagrees (edge_b < 3%), loses
    _insert_settled_leg(conn, "e2", "C", "D", 1.5, 1.6, 0.06, 0.01, SettlementResult.LOST, 0, 1)
    # Longshot bucket: both agree, wins big
    _insert_settled_leg(conn, "e3", "E", "F", 5.0, 5.5, 0.10, 0.08, SettlementResult.WON, 1, 0)
    # Longshot bucket: Method A flags, Method B disagrees, loses (the margin-bias scenario)
    _insert_settled_leg(conn, "e4", "G", "H", 6.0, 6.3, 0.05, -0.02, SettlementResult.LOST, 0, 2)

    report = evaluate(conn)

    assert report.overall_a.n == 4
    assert report.overall_b_agrees.n == 2  # only e1 and e3 had edge_b >= 3%

    fav_a = report.by_bucket_a["favorite"]
    assert fav_a.n == 2
    assert fav_a.total_profit == (1.9 - 1) + (-1.0)  # e1 won, e2 lost

    longshot_a = report.by_bucket_a["longshot"]
    assert longshot_a.n == 2
    assert longshot_a.hit_rate == 0.5  # 1 win, 1 loss

    fav_b = report.by_bucket_b_agrees["favorite"]
    assert fav_b.n == 1  # only e1
    assert fav_b.hit_rate == 1.0

    longshot_b = report.by_bucket_b_agrees["longshot"]
    assert longshot_b.n == 1  # only e3
    assert longshot_b.hit_rate == 1.0


def test_evaluate_empty_bucket_has_none_hit_rate(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    _insert_settled_leg(conn, "e1", "A", "B", 1.8, 1.9, 0.05, 0.04, SettlementResult.WON, 2, 0)

    report = evaluate(conn)

    assert report.by_bucket_a["mid"].n == 0
    assert report.by_bucket_a["mid"].hit_rate is None


def test_evaluate_push_excluded_from_hit_rate_but_counted(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    _insert_settled_leg(conn, "e1", "A", "B", 1.8, 1.9, 0.05, 0.04, SettlementResult.PUSH, 1, 1)
    _insert_settled_leg(conn, "e2", "C", "D", 1.8, 1.9, 0.05, 0.04, SettlementResult.WON, 2, 0)

    report = evaluate(conn)
    fav = report.by_bucket_a["favorite"]

    assert fav.n == 2
    assert fav.hit_rate == 1.0  # push excluded from denominator, only the win counts
    assert fav.total_profit == (1.9 - 1) + 0.0  # push contributes 0 profit
