from datetime import datetime, timezone

from vb.identity import content_hash_bytes
from vb.sources import results as results_module
from vb.sources.results import MIN_TEAM_SIMILARITY, _espn_slug_for, _team_similarity, find_result, find_result_with_evidence


def test_slug_for_top_flight_league():
    assert _espn_slug_for("France - Ligue 1") == "fra.1"


def test_slug_for_second_tier_league():
    assert _espn_slug_for("England - Championship") == "eng.2"


def test_slug_for_cup_competition():
    assert _espn_slug_for("Germany - DFB Pokal") == "ger.dfb_pokal"


def test_slug_for_international_competition_with_no_country_prefix():
    assert _espn_slug_for("UEFA - Champions League") == "uefa.champions"


def test_slug_for_champions_league_qualifiers_not_shadowed_by_bare_name():
    # "champions league" is a substring of "champions league qualifiers" -
    # the qualifiers entry must be checked first or this would misroute.
    assert _espn_slug_for("UEFA - Champions League Qualifiers") == "uefa.champions_qual"


def test_slug_for_known_uncovered_country_returns_none():
    assert _espn_slug_for("Poland - Ekstraklasa") is None


def test_slug_for_unrecognized_league_name_returns_none():
    assert _espn_slug_for("England - Some Obscure Regional Cup") is None


def test_slug_for_unrecognized_country_returns_none():
    assert _espn_slug_for("Narnia - Premier League") is None


def test_slug_for_competition_with_no_dash_and_no_international_match_returns_none():
    assert _espn_slug_for("Bundesliga") is None


def test_team_similarity_matches_common_abbreviation_via_alias_table():
    # Plain string similarity alone scores this ~70/100 - too close to
    # MIN_TEAM_SIMILARITY to trust; the alias table should resolve it cleanly.
    assert _team_similarity("Man City", "Manchester City") == 100.0


def test_team_similarity_low_for_unrelated_teams():
    assert _team_similarity("Arsenal", "Bournemouth") < 50


def test_team_similarity_low_for_derby_rivals_sharing_a_city_name():
    # Regression guard: token_set_ratio (tried and rejected - see module
    # docstring) scored this 100/100, which would have been a real
    # false-positive risk.
    assert _team_similarity("AC Milan", "Inter Milan") < MIN_TEAM_SIMILARITY


def test_slug_for_major_league_soccer():
    # Real coverage gap found 2026-07-28: usa.1 is a real, working ESPN
    # slug (confirmed live), but "major league soccer" was never listed
    # in _TOP_FLIGHT_NAMES - 10 real MLS matches sat unsettled.
    assert _espn_slug_for("USA - Major League Soccer") == "usa.1"


def test_slug_for_czech_first_liga():
    assert _espn_slug_for("Czech Republic - First Liga") == "cze.1"


def test_slug_for_romania_liga_1_with_arabic_numeral():
    # Pinnacle formats this with an arabic "1", not the roman numeral
    # "I" _TOP_FLIGHT_NAMES already had an entry for.
    assert _espn_slug_for("Romania - Liga 1") == "rou.1"


def test_slug_for_sweden_superettan_second_tier():
    assert _espn_slug_for("Sweden - Superettan") == "swe.2"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = str(payload).encode("utf-8")
        self.url = "https://site.api.espn.com/fake/scoreboard"

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _espn_event(home_name, away_name, home_score, away_score, status_name):
    return {
        "competitions": [{
            "status": {"type": {"completed": True, "name": status_name}},
            "competitors": [
                {"homeAway": "home", "team": {"displayName": home_name}, "score": str(home_score)},
                {"homeAway": "away", "team": {"displayName": away_name}, "score": str(away_score)},
            ],
        }]
    }


def test_find_result_accepts_a_match_decided_in_extra_time(monkeypatch):
    # Real gap found live 2026-07-29: STATUS_FINAL_AET has completed=True
    # and a real final score, but the old code required the status name
    # to be EXACTLY "STATUS_FULL_TIME" - a knockout match that went to
    # extra time sat unsettled forever despite ESPN having the result.
    payload = {"events": [_espn_event("Dinamo Zagreb", "FC Thun", 3, 2, "STATUS_FINAL_AET")]}
    monkeypatch.setattr(results_module.requests, "get", lambda *a, **k: _FakeResponse(payload))

    result = find_result(
        "UEFA - Champions League Qualifiers", "Dinamo Zagreb", "Thun",
        datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc),
    )
    assert result == (3, 2)


def test_find_result_accepts_a_match_decided_on_penalties_using_the_pre_shootout_score(monkeypatch):
    # STATUS_FINAL_PEN is the other status the old exact-match check
    # missed. ESPN's own `score` field for a penalty-shootout match
    # already reflects the pre-shootout (90+extra-time) goal count -
    # the correct value for standard 1X2/AH/Totals settlement, where
    # penalties only decide who advances, not the match result.
    # identical team names on both sides - isolates the status-name
    # behavior under test from the separate team-similarity concern
    # already covered by other tests in this file
    payload = {"events": [_espn_event("Celje", "Egnatia", 2, 2, "STATUS_FINAL_PEN")]}
    monkeypatch.setattr(results_module.requests, "get", lambda *a, **k: _FakeResponse(payload))

    result = find_result(
        "UEFA - Champions League Qualifiers", "Celje", "Egnatia",
        datetime(2026, 7, 28, 18, 15, tzinfo=timezone.utc),
    )
    assert result == (2, 2)


def test_find_result_still_rejects_a_live_or_scheduled_match(monkeypatch):
    payload = {"events": [_espn_event("Dinamo Zagreb", "FC Thun", 1, 0, "STATUS_IN_PROGRESS")]}
    monkeypatch.setattr(results_module.requests, "get", lambda *a, **k: _FakeResponse(payload))

    result = find_result(
        "UEFA - Champions League Qualifiers", "Dinamo Zagreb", "Thun",
        datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc),
    )
    assert result is None


def test_find_result_with_evidence_returns_hash_and_source_url_alongside_score(monkeypatch):
    payload = {"events": [_espn_event("Dinamo Zagreb", "FC Thun", 3, 2, "STATUS_FULL_TIME")]}
    monkeypatch.setattr(results_module.requests, "get", lambda *a, **k: _FakeResponse(payload))

    result = find_result_with_evidence(
        "UEFA - Champions League Qualifiers", "Dinamo Zagreb", "Thun",
        datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc),
    )

    assert result is not None
    assert result.score == (3, 2)
    assert result.raw_payload_hash == content_hash_bytes(str(payload).encode("utf-8"))
    assert result.source_url == "https://site.api.espn.com/fake/scoreboard"


def test_find_result_with_evidence_returns_none_when_unmatched(monkeypatch):
    payload = {"events": [_espn_event("Dinamo Zagreb", "FC Thun", 1, 0, "STATUS_IN_PROGRESS")]}
    monkeypatch.setattr(results_module.requests, "get", lambda *a, **k: _FakeResponse(payload))

    assert find_result_with_evidence(
        "UEFA - Champions League Qualifiers", "Dinamo Zagreb", "Thun",
        datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc),
    ) is None


def test_team_similarity_matches_short_pinnacle_name_via_new_aliases():
    # Confirmed live 2026-07-28 against real unsettled matches: Pinnacle's
    # short name vs ESPN's fuller/native-language displayName scored
    # 55-78/100 without these aliases - well below MIN_TEAM_SIMILARITY.
    assert _team_similarity("Kalmar", "Kalmar FF") >= MIN_TEAM_SIMILARITY
    assert _team_similarity("Zhejiang", "Zhejiang Professional FC") >= MIN_TEAM_SIMILARITY
    assert _team_similarity("FC Copenhagen", "F.C. København") >= MIN_TEAM_SIMILARITY
