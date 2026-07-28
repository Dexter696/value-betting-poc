from vb.sources.results import MIN_TEAM_SIMILARITY, _espn_slug_for, _team_similarity


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


def test_team_similarity_matches_short_pinnacle_name_via_new_aliases():
    # Confirmed live 2026-07-28 against real unsettled matches: Pinnacle's
    # short name vs ESPN's fuller/native-language displayName scored
    # 55-78/100 without these aliases - well below MIN_TEAM_SIMILARITY.
    assert _team_similarity("Kalmar", "Kalmar FF") >= MIN_TEAM_SIMILARITY
    assert _team_similarity("Zhejiang", "Zhejiang Professional FC") >= MIN_TEAM_SIMILARITY
    assert _team_similarity("FC Copenhagen", "F.C. København") >= MIN_TEAM_SIMILARITY
