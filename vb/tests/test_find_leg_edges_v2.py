from datetime import datetime, timezone

from vb.matching import MatchTier
from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from vb.pipeline import find_leg_edges_v2, market_key

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def _event(site, home, away, event_id=None, competition="Champions League", kickoff=T0):
    return RawEvent(
        site=site, sport="soccer", competition=competition, kickoff_utc=kickoff,
        raw_home_team=home, raw_away_team=away, event_id=event_id or f"{site}:{home}-{away}",
    )


def _moneyline(event, home_odds, draw_odds, away_odds, captured_at=T0):
    return MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, home_odds), Outcome(Selection.DRAW, draw_odds), Outcome(Selection.AWAY, away_odds)),
        captured_at=captured_at,
    )


def _handicap(event, line, home_odds, away_odds, captured_at=T0):
    return MarketSnapshot(
        event=event, market_type=MarketType.ASIAN_HANDICAP, line=line,
        outcomes=(Outcome(Selection.HOME, home_odds), Outcome(Selection.AWAY, away_odds)),
        captured_at=captured_at,
    )


def test_find_leg_edges_v2_matches_a_normal_same_orientation_pair_like_v1_would():
    anchor = _event("pinnacle.com", "Liverpool", "Everton", event_id="p1")
    candidate = _event("swisslos.ch", "Liverpool", "Everton", event_id="s1")

    benchmark = [_moneyline(anchor, 2.10, 3.40, 3.60)]
    comparison = [_moneyline(candidate, 2.30, 3.40, 3.60)]

    legs = find_leg_edges_v2(benchmark, comparison)

    home_leg = next(l for l in legs if l.selection == Selection.HOME)
    assert home_leg.benchmark_odds == 2.10
    assert home_leg.comparison_odds == 2.30


def test_f14_a_swapped_home_away_pair_is_correctly_remapped_not_silently_mismatched():
    # The candidate site lists the SAME fixture with home/away reversed
    # (a real, if rare, real-world naming difference between sites).
    # Anchor: Liverpool(home)/Everton(away). Candidate: Everton(home)/
    # Liverpool(away), with its OWN "home" price (Everton, 3.50) and
    # "away" price (Liverpool, 2.30).
    #
    # Without orientation remapping, candidate's raw HOME (Everton,
    # 3.50) would get paired against the anchor's HOME (Liverpool,
    # 2.10) - producing a wildly overstated, WRONG "edge" on a leg that
    # doesn't actually exist (comparing two different teams' prices).
    # With the fix, candidate's raw HOME (Everton) remaps to the
    # anchor's AWAY slot, and candidate's raw AWAY (Liverpool) remaps
    # to the anchor's HOME slot - so the anchor's real HOME team
    # (Liverpool) is correctly compared against the candidate's real
    # Liverpool price (2.30), not Everton's.
    anchor = _event("pinnacle.com", "Liverpool", "Everton", event_id="p1")
    candidate = _event("swisslos.ch", "Everton", "Liverpool", event_id="s1")  # reversed team order

    benchmark = [_moneyline(anchor, 2.10, 3.40, 3.60)]
    comparison = [_moneyline(candidate, 3.50, 3.40, 2.30)]  # candidate's HOME=Everton@3.50, AWAY=Liverpool@2.30

    legs = find_leg_edges_v2(benchmark, comparison)

    home_leg = next(l for l in legs if l.selection == Selection.HOME)
    assert home_leg.benchmark_odds == 2.10       # anchor's real Liverpool price
    assert home_leg.comparison_odds == 2.30       # candidate's real Liverpool price, correctly matched
    assert round(home_leg.edge_a, 4) == round(2.30 / 2.10 - 1, 4)

    away_leg = next(l for l in legs if l.selection == Selection.AWAY)
    assert away_leg.benchmark_odds == 3.60        # anchor's real Everton price
    assert away_leg.comparison_odds == 3.50       # candidate's real Everton price, correctly matched


def test_f14_a_swapped_handicap_line_sign_is_flipped_before_comparison():
    # Anchor's home team (Liverpool) is a -1.0 favorite. The candidate
    # site's raw line is signed from ITS OWN (swapped) home team's
    # perspective - i.e. "+1.0" from Everton's perspective, which is
    # the same real-world line as Liverpool -1.0. Without remapping,
    # +1.0 would be compared directly against the anchor's -1.0 line
    # and never match at all (match_markets requires equal lines).
    anchor = _event("pinnacle.com", "Liverpool", "Everton", event_id="p1")
    candidate = _event("swisslos.ch", "Everton", "Liverpool", event_id="s1")

    benchmark = [_handicap(anchor, -1.0, 1.90, 1.95)]
    comparison = [_handicap(candidate, 1.0, 1.92, 1.93)]  # candidate's HOME(Everton)=+1.0@1.92, AWAY(Liverpool)=-1.0-equivalent@1.93

    legs = find_leg_edges_v2(benchmark, comparison)

    assert len(legs) == 2
    home_leg = next(l for l in legs if l.selection == Selection.HOME)
    assert home_leg.line == -1.0
    assert home_leg.comparison_odds == 1.93  # candidate's Liverpool(-1.0-equivalent) price, correctly matched


def test_find_leg_edges_v2_uses_global_bipartite_assignment_not_greedy():
    # Same audit example already proven at the event-matching level
    # (test_market_mapping.py) - re-verified end-to-end through the
    # actual edge-computation entry point used by run_cycle_v2.
    from datetime import timedelta

    a = _event("pinnacle.com", "Liverpool", "Everton", event_id="pA", kickoff=T0)
    b = _event("pinnacle.com", "Liverpool", "Everton", event_id="pB", kickoff=T0 + timedelta(minutes=12))
    x = _event("swisslos.ch", "Liverpool", "Everton", event_id="sX", kickoff=T0 + timedelta(minutes=2))
    y = _event("swisslos.ch", "Liverpool", "Everton", event_id="sY", kickoff=T0 - timedelta(minutes=5))

    benchmark = [_moneyline(a, 2.10, 3.40, 3.60), _moneyline(b, 2.10, 3.40, 3.60)]
    comparison = [_moneyline(x, 2.30, 3.40, 3.60), _moneyline(y, 2.30, 3.40, 3.60)]

    legs = find_leg_edges_v2(benchmark, comparison)
    matched_event_ids = {(l.benchmark_event.event_id, l.comparison_event.event_id) for l in legs}
    assert matched_event_ids == {("pA", "sY"), ("pB", "sX")}
