from datetime import datetime, timedelta, timezone

from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from vb.opportunity import ResolutionReason
from vb.pipeline import find_leg_edges, full_market_json, market_key, run_cycle
from vb.storage import init_db, save_raw_capture

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
CAPTURED = T0 - timedelta(hours=1)


def _pinnacle_event(event_id="p1"):
    return RawEvent(
        site="pinnacle.com", sport="soccer", competition="Premier League",
        kickoff_utc=T0, raw_home_team="Liverpool", raw_away_team="Everton", event_id=event_id,
    )


def _swisslos_event(event_id="s1"):
    return RawEvent(
        site="swisslos.ch", sport="soccer", competition="Premier League",
        kickoff_utc=T0, raw_home_team="Liverpool", raw_away_team="Everton", event_id=event_id,
    )


def _moneyline(event, home, draw, away, captured_at=CAPTURED, max_bet_size=None):
    return MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, home), Outcome(Selection.DRAW, draw), Outcome(Selection.AWAY, away)),
        captured_at=captured_at, max_bet_size=max_bet_size,
    )


def test_find_leg_edges_matches_and_computes_both_methods():
    benchmark = [_moneyline(_pinnacle_event(), 2.10, 3.40, 3.60, max_bet_size=500)]
    comparison = [_moneyline(_swisslos_event(), 2.30, 3.40, 3.60, max_bet_size=200)]

    legs = find_leg_edges(benchmark, comparison)

    home_leg = next(l for l in legs if l.selection == Selection.HOME)
    assert round(home_leg.edge_a, 4) == 0.0952
    assert round(home_leg.edge_b, 4) == 0.045
    assert home_leg.benchmark_odds == 2.10
    assert home_leg.comparison_odds == 2.30
    assert home_leg.max_bet_size == 200
    assert home_leg.market_key == market_key(_pinnacle_event(), MarketType.MATCH_WINNER, None, Selection.HOME, "swisslos.ch")


def test_find_leg_edges_skips_unmatched_events():
    benchmark = [_moneyline(_pinnacle_event(), 2.10, 3.40, 3.60)]
    unrelated = RawEvent(
        site="swisslos.ch", sport="soccer", competition="La Liga",
        kickoff_utc=T0, raw_home_team="Real Madrid", raw_away_team="Barcelona", event_id="s2",
    )
    comparison = [_moneyline(unrelated, 2.30, 3.40, 3.60)]

    assert find_leg_edges(benchmark, comparison) == []


def test_full_market_json_includes_both_books_and_overround():
    benchmark = [_moneyline(_pinnacle_event(), 2.10, 3.40, 3.60)]
    comparison = [_moneyline(_swisslos_event(), 2.30, 3.40, 3.60)]
    leg = find_leg_edges(benchmark, comparison)[0]

    fm = full_market_json(leg)
    assert fm["benchmark"]["site"] == "pinnacle.com"
    assert fm["comparison"]["site"] == "swisslos.ch"
    assert fm["benchmark"]["overround"] > 0
    assert len(fm["benchmark"]["outcomes"]) == 3


def test_run_cycle_persists_and_resumes_across_calls(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    pinnacle_event = _pinnacle_event()
    swisslos_event = _swisslos_event()

    save_raw_capture(conn, pinnacle_event, [_moneyline(pinnacle_event, 2.10, 3.40, 3.60, captured_at=CAPTURED)])
    save_raw_capture(
        conn, swisslos_event,
        [_moneyline(swisslos_event, 2.30, 3.40, 3.60, captured_at=CAPTURED, max_bet_size=200)],
    )

    touched = run_cycle(conn, "pinnacle.com", "swisslos.ch", now=CAPTURED)
    home_opp = next(o for o in touched if o.selection == Selection.HOME)
    assert home_opp.is_open
    assert len(home_opp.snapshots) == 1
    assert round(home_opp.entry_edge_a, 4) == 0.0952

    # second cycle: comparison odds drop, same leg should converge and close
    # on the SAME instance rather than opening a new one.
    save_raw_capture(
        conn, swisslos_event,
        [_moneyline(swisslos_event, 2.00, 3.40, 3.60, captured_at=CAPTURED + timedelta(minutes=1), max_bet_size=200)],
    )
    touched_again = run_cycle(conn, "pinnacle.com", "swisslos.ch", now=CAPTURED + timedelta(minutes=1))
    home_opp_2 = next(o for o in touched_again if o.selection == Selection.HOME)

    assert home_opp_2.instance_id == home_opp.instance_id  # same instance, resumed
    assert not home_opp_2.is_open
    assert home_opp_2.resolution_reason == ResolutionReason.DROPPED_BELOW_THRESHOLD
    assert len(home_opp_2.snapshots) == 2


def test_run_cycle_closes_opportunity_once_kickoff_passes_even_with_frozen_odds(tmp_path):
    # Reproduces the real gap found live: load_latest_market_snapshots
    # keeps returning the same last-known snapshot for an event forever,
    # even after the source site has dropped it from its feed post-
    # kickoff, so the edge never changes and "dropped_below_threshold"
    # never fires. Kickoff time passing must close the opportunity on its
    # own, independent of whether the (frozen) edge is still above
    # threshold.
    conn = init_db(tmp_path / "vb.sqlite")

    pinnacle_event = _pinnacle_event()
    swisslos_event = _swisslos_event()

    save_raw_capture(conn, pinnacle_event, [_moneyline(pinnacle_event, 2.10, 3.40, 3.60, captured_at=CAPTURED)])
    save_raw_capture(
        conn, swisslos_event,
        [_moneyline(swisslos_event, 2.30, 3.40, 3.60, captured_at=CAPTURED, max_bet_size=200)],
    )

    # Cycle 1: before kickoff, opens normally.
    touched = run_cycle(conn, "pinnacle.com", "swisslos.ch", now=CAPTURED)
    home_opp = next(o for o in touched if o.selection == Selection.HOME)
    assert home_opp.is_open

    # Cycle 2: no new capture happened (simulating the site having
    # dropped the match from its feed) - same frozen snapshots are still
    # "latest", so the edge is unchanged and still above threshold. But
    # `now` is now past T0 (kickoff).
    touched_again = run_cycle(conn, "pinnacle.com", "swisslos.ch", now=T0 + timedelta(minutes=5))
    home_opp_2 = next(o for o in touched_again if o.selection == Selection.HOME)

    assert home_opp_2.instance_id == home_opp.instance_id
    assert not home_opp_2.is_open
    assert home_opp_2.resolution_reason == ResolutionReason.EVENT_STARTED
    assert home_opp_2.entry_edge_a == home_opp.entry_edge_a  # edge never actually moved
