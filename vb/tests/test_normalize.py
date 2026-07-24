from vb.normalize import canonical_handicap_line, normalize_team_name


def test_accent_stripping():
    assert normalize_team_name("Fenerbahçe") == normalize_team_name("Fenerbahce")


def test_club_suffix_stripped():
    assert normalize_team_name("FC Basel") == normalize_team_name("Basel")


def test_french_club_alias():
    # Swisslos/Loro may render Basel by its French name "Bâle".
    assert normalize_team_name("FC Bâle") == normalize_team_name("Basel")


def test_french_country_translation():
    assert normalize_team_name("Allemagne") == normalize_team_name("Germany")
    assert normalize_team_name("Angleterre") == normalize_team_name("England")


def test_distinct_teams_stay_distinct():
    assert normalize_team_name("Liverpool") != normalize_team_name("Everton")


def test_canonical_handicap_line_home_perspective():
    # "home -1.5" and "away +1.5" are the same line.
    assert canonical_handicap_line("home", -1.5) == -1.5
    assert canonical_handicap_line("away", 1.5) == -1.5


def test_canonical_handicap_line_rejects_bad_side():
    import pytest

    with pytest.raises(ValueError):
        canonical_handicap_line("draw", 0.0)
