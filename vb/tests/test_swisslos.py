from datetime import datetime
from zoneinfo import ZoneInfo

from vb.sources.swisslos import _competition_name, _parse_kickoff

ZURICH = ZoneInfo("Europe/Zurich")


def test_parse_kickoff_heute():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("Heute • 18:00", now_local)
    assert kickoff.isoformat() == "2026-07-23T16:00:00+00:00"  # CEST = UTC+2


def test_parse_kickoff_morgen():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("Morgen • 09:30", now_local)
    assert kickoff.isoformat() == "2026-07-24T07:30:00+00:00"


def test_parse_kickoff_explicit_date_same_year():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("30.08 • 20:45", now_local)
    assert kickoff.year == 2026
    assert kickoff.month == 8 and kickoff.day == 30


def test_parse_kickoff_weekday_prefixed_non_padded_date():
    # Seen on the "Alle Bewerbe" view for further-out fixtures: weekday
    # abbreviation + non-zero-padded day/month, e.g. "Sa. 25.7" (Saturday).
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("Sa. 25.7 • 18:00", now_local)
    assert kickoff.month == 7 and kickoff.day == 25
    assert kickoff.hour == 16  # 18:00 CEST -> 16:00 UTC


def test_parse_kickoff_explicit_date_rolls_to_next_year():
    # A date earlier than "yesterday" relative to now must mean next year's
    # occurrence (e.g. seeing "02.01" in December means next January).
    now_local = datetime(2026, 12, 20, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("02.01 • 15:00", now_local)
    assert kickoff.year == 2027
    assert kickoff.month == 1 and kickoff.day == 2


def test_parse_kickoff_unrecognized_format_returns_none():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    assert _parse_kickoff("Irgendwas komisch", now_local) is None


def test_competition_name_strips_trailing_column_labels():
    raw = "Qualifikation, Europa League1  X  2  Over  Tore  Under"
    assert _competition_name(raw) == "Qualifikation, Europa League"


def test_competition_name_strips_trailing_count():
    assert _competition_name("Klub-Freundschaftsspiele10") == "Klub-Freundschaftsspiele"
