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


def test_nordic_club_suffix_stripped():
    # The actual miss that motivated adding "if"/"is" as stopwords
    # (vb/sources/results.py's ESPN settlement, 2026-07-25): Pinnacle's
    # "Orgryte" vs ESPN's "Orgryte IS" scored 82/100 similarity before
    # this fix (below the 88 settlement safety bar) purely because of
    # the untranslated Swedish suffix ("IS" = Idrottssallskap).
    assert normalize_team_name("Orgryte IS") == normalize_team_name("Orgryte")
    assert normalize_team_name("Djurgardens IF") == normalize_team_name("Djurgardens")


def test_nordic_club_suffix_stripping_does_not_collide_distinct_clubs():
    # Guards against the risk flagged in review: stripping "if"/"is" as
    # whole tokens is shared by vb.matching (pre-bet cross-site event
    # matching, where "a mismatched market produces fake value") as well
    # as ESPN settlement, so two genuinely different clubs must not
    # normalize to the same string just because both carry one of these
    # common Nordic suffixes.
    assert normalize_team_name("Djurgardens IF") != normalize_team_name("Hammarby IF")
    assert normalize_team_name("Orgryte IS") != normalize_team_name("Djurgardens IF")


def test_canonical_handicap_line_home_perspective():
    # "home -1.5" and "away +1.5" are the same line.
    assert canonical_handicap_line("home", -1.5) == -1.5
    assert canonical_handicap_line("away", 1.5) == -1.5


def test_canonical_handicap_line_rejects_bad_side():
    import pytest

    with pytest.raises(ValueError):
        canonical_handicap_line("draw", 0.0)
