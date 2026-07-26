from datetime import datetime, timedelta, timezone

from vb.market_mapping import (
    match_events_v2,
    remap_handicap_line,
    remap_selection,
    score_oriented_pair,
)
from vb.matching import MatchTier, match_events
from vb.models import MatchOrientation, RawEvent, Selection

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def _event(site, home, away, kickoff=T0, competition="Champions League", event_id=None):
    return RawEvent(
        site=site, sport="soccer", competition=competition, kickoff_utc=kickoff,
        raw_home_team=home, raw_away_team=away, event_id=event_id or f"{site}:{home}-{away}:{kickoff.isoformat()}",
    )


def test_f14_global_assignment_beats_greedy_on_the_audits_own_example():
    # The audit's exact scenario: anchor A's best candidate (X) is also
    # anchor B's ONLY viable candidate. Greedy lets A claim X first
    # (since 1.0 > 0.97) and leaves B with nothing, even though
    # reassigning A to its second-best candidate (Y) and giving B its
    # only candidate (X) scores higher overall AND matches both anchors.
    a = _event("pinnacle.com", "Liverpool", "Everton", T0, event_id="pA")
    b = _event("pinnacle.com", "Liverpool", "Everton", T0 + timedelta(minutes=12), event_id="pB")
    x = _event("swisslos.ch", "Liverpool", "Everton", T0 + timedelta(minutes=2), event_id="sX")
    y = _event("swisslos.ch", "Liverpool", "Everton", T0 - timedelta(minutes=5), event_id="sY")

    greedy = match_events([a, b], [x, y])
    assert len(greedy) == 1
    assert greedy[0].anchor.event_id == "pA"
    assert greedy[0].candidate.event_id == "sX"  # greedy's local-best pick strands B

    optimal = match_events_v2([a, b], [x, y])
    assignment = {m.anchor.event_id: m.candidate.event_id for m in optimal}
    assert assignment == {"pA": "sY", "pB": "sX"}  # global optimum reassigns A, matches both


def test_unrelated_events_in_different_blocks_never_compete():
    a = _event("pinnacle.com", "Liverpool", "Everton", T0, event_id="pA")
    x = _event("swisslos.ch", "Liverpool", "Everton", T0, event_id="sX")
    unrelated_b = _event("pinnacle.com", "Real Madrid", "Barcelona", T0 + timedelta(hours=6), event_id="pB")
    unrelated_y = _event("swisslos.ch", "Real Madrid", "Barcelona", T0 + timedelta(hours=6), event_id="sY")

    matches = match_events_v2([a, unrelated_b], [x, unrelated_y])
    assignment = {m.anchor.event_id: m.candidate.event_id for m in matches}
    assert assignment == {"pA": "sX", "pB": "sY"}


def test_score_oriented_pair_detects_a_decisive_swap_and_records_it_as_data():
    anchor = _event("pinnacle.com", "Liverpool", "Everton", event_id="pA")
    swapped_candidate = _event("swisslos.ch", "Everton", "Liverpool", event_id="sX")

    m = score_oriented_pair(anchor, swapped_candidate)

    assert m is not None
    assert m.orientation is MatchOrientation.SWAPPED
    assert m.tier == MatchTier.AUTO  # decisive swap, not ambiguous - safe to auto-accept with remapping
    assert any("swapped" in r for r in m.reasons)


def test_score_oriented_pair_direct_orientation_for_a_normal_match():
    anchor = _event("pinnacle.com", "Liverpool", "Everton", event_id="pA")
    candidate = _event("swisslos.ch", "Liverpool", "Everton", event_id="sX")

    m = score_oriented_pair(anchor, candidate)

    assert m is not None
    assert m.orientation is MatchOrientation.SAME


def test_match_events_v2_carries_orientation_through_into_the_result():
    a = _event("pinnacle.com", "Liverpool", "Everton", event_id="pA")
    swapped_x = _event("swisslos.ch", "Everton", "Liverpool", event_id="sX")

    matches = match_events_v2([a], [swapped_x])

    assert len(matches) == 1
    assert matches[0].orientation is MatchOrientation.SWAPPED


def test_remap_selection_swaps_home_and_away_only_when_orientation_is_swapped():
    assert remap_selection(Selection.HOME, MatchOrientation.SAME) == Selection.HOME
    assert remap_selection(Selection.HOME, MatchOrientation.SWAPPED) == Selection.AWAY
    assert remap_selection(Selection.AWAY, MatchOrientation.SWAPPED) == Selection.HOME
    assert remap_selection(Selection.DRAW, MatchOrientation.SWAPPED) == Selection.DRAW
    assert remap_selection(Selection.OVER, MatchOrientation.SWAPPED) == Selection.OVER
    assert remap_selection(Selection.UNDER, MatchOrientation.SWAPPED) == Selection.UNDER


def test_remap_handicap_line_flips_sign_only_when_orientation_is_swapped():
    assert remap_handicap_line(-1.5, MatchOrientation.SAME) == -1.5
    assert remap_handicap_line(-1.5, MatchOrientation.SWAPPED) == 1.5
    assert remap_handicap_line(None, MatchOrientation.SWAPPED) is None
