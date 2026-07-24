from datetime import datetime, timedelta, timezone

from vb.matching import MatchTier, match_events, match_markets, score_event_pair
from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from vb.normalize import canonical_handicap_line

KICKOFF = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def _event(site, home, away, competition, kickoff=KICKOFF, sport="soccer", event_id=None):
    return RawEvent(
        site=site,
        sport=sport,
        competition=competition,
        kickoff_utc=kickoff,
        raw_home_team=home,
        raw_away_team=away,
        event_id=event_id or f"{site}:{home}-{away}",
    )


def test_auto_match_same_language():
    anchor = _event("pinnacle.com", "Liverpool", "Everton", "Premier League")
    candidate = _event("betfair.com", "Liverpool", "Everton", "English Premier League")

    m = score_event_pair(anchor, candidate)

    assert m is not None
    assert m.tier == MatchTier.AUTO


def test_auto_match_swiss_french_naming():
    anchor = _event("pinnacle.com", "FC Basel", "BSC Young Boys", "Swiss Super League")
    candidate = _event("swisslos.ch", "Bâle", "Young Boys", "Super League Suisse")

    m = score_event_pair(anchor, candidate)

    assert m is not None
    assert m.tier == MatchTier.AUTO


def test_auto_match_national_team_french_translation():
    anchor = _event("pinnacle.com", "Germany", "Spain", "UEFA Nations League")
    candidate = _event("loro.ch", "Allemagne", "Espagne", "Ligue des Nations UEFA")

    m = score_event_pair(anchor, candidate)

    assert m is not None
    assert m.tier in (MatchTier.AUTO, MatchTier.REVIEW)


def test_time_outside_tolerance_hard_rejected():
    anchor = _event("pinnacle.com", "Liverpool", "Everton", "Premier League")
    # Same teams, same competition, but 6 hours later - a different fixture
    # (or a data error), not the same match no matter how similar names are.
    candidate = _event(
        "betfair.com", "Liverpool", "Everton", "Premier League",
        kickoff=KICKOFF + timedelta(hours=6),
    )

    assert score_event_pair(anchor, candidate) is None


def test_home_away_swap_forces_review_not_auto():
    anchor = _event("pinnacle.com", "Liverpool", "Everton", "Premier League")
    candidate = _event("betfair.com", "Everton", "Liverpool", "Premier League")

    m = score_event_pair(anchor, candidate)

    assert m is not None
    assert m.tier == MatchTier.REVIEW
    assert any("swapped" in r for r in m.reasons)


def test_unrelated_teams_rejected():
    anchor = _event("pinnacle.com", "Liverpool", "Everton", "Premier League")
    candidate = _event("betfair.com", "Random Town FC", "Other City", "Premier League")

    assert score_event_pair(anchor, candidate) is None


def test_match_events_greedy_one_to_one():
    anchors = [
        _event("pinnacle.com", "Liverpool", "Everton", "Premier League", event_id="p1"),
        _event(
            "pinnacle.com", "Arsenal", "Chelsea", "Premier League",
            kickoff=KICKOFF + timedelta(hours=2), event_id="p2",
        ),
    ]
    candidates = [
        _event("swisslos.ch", "Liverpool", "Everton", "Premier League", event_id="s1"),
        _event(
            "swisslos.ch", "Arsenal", "Chelsea", "Premier League",
            kickoff=KICKOFF + timedelta(hours=2), event_id="s2",
        ),
    ]

    results = match_events(anchors, candidates)

    assert {r.anchor.event_id: r.candidate.event_id for r in results} == {"p1": "s1", "p2": "s2"}


def test_match_markets_handicap_perspective_agnostic():
    anchor_event = _event("pinnacle.com", "Basel", "Young Boys", "Swiss Super League")
    candidate_event = _event("swisslos.ch", "Bâle", "Young Boys", "Super League Suisse")

    # Pinnacle quotes home -1.5; Swisslos quotes it as away +1.5. Both are
    # canonicalized to line=-1.5 (home perspective) at ingestion time.
    anchor_market = MarketSnapshot(
        event=anchor_event,
        market_type=MarketType.ASIAN_HANDICAP,
        line=canonical_handicap_line("home", -1.5),
        outcomes=(Outcome(Selection.HOME, 1.90), Outcome(Selection.AWAY, 1.95)),
        captured_at=KICKOFF - timedelta(hours=1),
    )
    candidate_market = MarketSnapshot(
        event=candidate_event,
        market_type=MarketType.ASIAN_HANDICAP,
        line=canonical_handicap_line("away", 1.5),
        outcomes=(Outcome(Selection.HOME, 1.85), Outcome(Selection.AWAY, 2.00)),
        captured_at=KICKOFF - timedelta(hours=1),
    )

    matches, unmatched = match_markets([anchor_market], [candidate_market])

    assert len(matches) == 1
    assert not unmatched
    assert matches[0].anchor.line == matches[0].candidate.line == -1.5


def test_match_markets_totals_line_must_match_exactly():
    event = _event("pinnacle.com", "Basel", "Young Boys", "Swiss Super League")
    anchor_market = MarketSnapshot(
        event=event, market_type=MarketType.TOTALS, line=2.5,
        outcomes=(Outcome(Selection.OVER, 1.90), Outcome(Selection.UNDER, 1.90)),
        captured_at=KICKOFF,
    )
    same_line = MarketSnapshot(
        event=event, market_type=MarketType.TOTALS, line=2.5,
        outcomes=(Outcome(Selection.OVER, 1.85), Outcome(Selection.UNDER, 1.95)),
        captured_at=KICKOFF,
    )
    different_line = MarketSnapshot(
        event=event, market_type=MarketType.TOTALS, line=2.75,
        outcomes=(Outcome(Selection.OVER, 1.90), Outcome(Selection.UNDER, 1.90)),
        captured_at=KICKOFF,
    )

    matches, unmatched = match_markets([anchor_market], [different_line])
    assert not matches
    assert unmatched == [anchor_market]

    matches, unmatched = match_markets([anchor_market], [same_line])
    assert len(matches) == 1
    assert not unmatched
