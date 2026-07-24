from datetime import datetime
from zoneinfo import ZoneInfo

from vb.sources.loro import _parse_kickoff

ZURICH = ZoneInfo("Europe/Zurich")


def test_parse_kickoff_aujourdhui():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("aujourd'hui", "18:00", now_local)
    assert kickoff.isoformat() == "2026-07-23T16:00:00+00:00"


def test_parse_kickoff_demain():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("demain", "09:30", now_local)
    assert kickoff.isoformat() == "2026-07-24T07:30:00+00:00"


def test_parse_kickoff_weekday_date():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("sam. 25.07", "18:00", now_local)
    assert kickoff.month == 7 and kickoff.day == 25


def test_parse_kickoff_rolls_to_next_year():
    now_local = datetime(2026, 12, 20, 10, 0, tzinfo=ZURICH)
    kickoff = _parse_kickoff("ven. 02.01", "15:00", now_local)
    assert kickoff.year == 2027
    assert kickoff.month == 1 and kickoff.day == 2


def test_parse_kickoff_unrecognized_format_returns_none():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    assert _parse_kickoff("blah", "18:00", now_local) is None


def test_parse_kickoff_bad_time_returns_none():
    now_local = datetime(2026, 7, 23, 10, 0, tzinfo=ZURICH)
    assert _parse_kickoff("aujourd'hui", "not-a-time", now_local) is None
