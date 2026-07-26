from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from vb.models import MarketType
from vb.sources.swisslos import (
    MAX_STAKE_CHF,
    SwisslosClient,
    _competition_name,
    _parse_asian_handicap_section,
    _parse_kickoff,
)

ZURICH = ZoneInfo("Europe/Zurich")


class _FakeRowElement:
    """Stands in for a Playwright ElementHandle for _parse_row tests -
    only the methods _parse_row actually calls."""

    def __init__(self, row_id: str, texts: list[str], detail_href: str | None = None):
        self._row_id = row_id
        self._texts = texts
        self._detail_href = detail_href

    def get_attribute(self, name):
        assert name == "id"
        return self._row_id

    def eval_on_selector_all(self, selector, script):
        return self._texts

    def query_selector(self, selector):
        assert selector == "a"
        return _FakeLinkElement(self._detail_href) if self._detail_href else None


class _FakeLinkElement:
    def __init__(self, href: str):
        self._href = href

    def get_attribute(self, name):
        assert name == "href"
        return self._href


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


def test_competition_name_preserves_a_leading_digit_in_the_name():
    # Audit F-14: the old "strip from the first digit anywhere" approach
    # destroyed Swiss league-tier names that themselves start with a
    # digit, since it treated "2" in "2. Bundesliga" the same as the
    # digit that kicks off the glued column labels.
    raw = "2. Bundesliga1  X  2  Over  Tore  Under"
    assert _competition_name(raw) == "2. Bundesliga"


def test_competition_name_preserves_a_leading_digit_with_no_trailing_labels():
    assert _competition_name("3. Liga") == "3. Liga"


def test_parse_row_handles_match_winner_only_row():
    # Reproduces a real gap found live: lower-tier competitions (e.g.
    # DFB-Pokal) only ever show match-winner odds, no totals column - a
    # 7-item row, not the 10-item pattern most competitions have. This
    # used to be silently dropped entirely.
    row_el = _FakeRowElement(
        "sportsSportsGrid_row_0_asw:event:abc123",
        ["Preussen Munster", "Karlsruhe", "Fr. 21.8 • 18:00", "• 1 >>", "2.75", "3.25", "2.30"],
    )
    now_local = datetime(2026, 8, 1, 10, 0, tzinfo=ZURICH)

    row = SwisslosClient._parse_row(row_el, "DFB-Pokal", "soccer", datetime.now(timezone.utc), now_local)

    assert row is not None
    assert len(row.snapshots) == 1
    assert row.snapshots[0].market_type == MarketType.MATCH_WINNER
    odds = {o.selection.value: o.odds for o in row.snapshots[0].outcomes}
    assert odds == {"home": 2.75, "draw": 3.25, "away": 2.30}


def test_parse_row_still_handles_full_ten_item_row():
    row_el = _FakeRowElement(
        "sportsSportsGrid_row_0_asw:event:xyz789",
        ["Dortmund", "Bayern Munich", "Sa. 22.8 • 20:30", "• 124 >>", "4.80", "4.30", "1.62", "1.83", "3.5", "1.90"],
    )
    now_local = datetime(2026, 8, 1, 10, 0, tzinfo=ZURICH)

    row = SwisslosClient._parse_row(row_el, "Bundesliga", "soccer", datetime.now(timezone.utc), now_local)

    assert row is not None
    assert len(row.snapshots) == 2
    market_types = {s.market_type for s in row.snapshots}
    assert market_types == {MarketType.MATCH_WINNER, MarketType.TOTALS}


def test_parse_row_returns_none_for_too_few_items():
    row_el = _FakeRowElement(
        "sportsSportsGrid_row_0_asw:event:short1",
        ["Team A", "Team B", "Heute • 18:00"],  # no odds at all yet
    )
    now_local = datetime(2026, 8, 1, 10, 0, tzinfo=ZURICH)

    row = SwisslosClient._parse_row(row_el, "Some League", "soccer", datetime.now(timezone.utc), now_local)

    assert row is None


def test_parse_row_sets_flat_max_stake_on_every_snapshot():
    # Swisslos has no per-market max stake like Pinnacle - just a flat,
    # site-wide cap confirmed live via the bet slip's own validation
    # message (see MAX_STAKE_CHF docstring) - every snapshot should carry it.
    row_el = _FakeRowElement(
        "sportsSportsGrid_row_0_asw:event:xyz789",
        ["Dortmund", "Bayern Munich", "Sa. 22.8 • 20:30", "• 124 >>", "4.80", "4.30", "1.62", "1.83", "3.5", "1.90"],
    )
    now_local = datetime(2026, 8, 1, 10, 0, tzinfo=ZURICH)

    row = SwisslosClient._parse_row(row_el, "Bundesliga", "soccer", datetime.now(timezone.utc), now_local)

    assert row is not None
    assert all(s.max_bet_size == MAX_STAKE_CHF for s in row.snapshots)


def test_parse_row_captures_detail_url_from_row_link():
    row_el = _FakeRowElement(
        "sportsSportsGrid_row_0_asw:event:xyz789",
        ["Dortmund", "Bayern Munich", "Sa. 22.8 • 20:30", "• 124 >>", "4.80", "4.30", "1.62"],
        detail_href="https://www.swisslos.ch/de/sporttip/sportwetten/fussball/deutschland/bundesliga/dortmund-vs-bayern-munich?t=123",
    )
    now_local = datetime(2026, 8, 1, 10, 0, tzinfo=ZURICH)

    row = SwisslosClient._parse_row(row_el, "Bundesliga", "soccer", datetime.now(timezone.utc), now_local)

    assert row is not None
    assert row.detail_url == "https://www.swisslos.ch/de/sporttip/sportwetten/fussball/deutschland/bundesliga/dortmund-vs-bayern-munich?t=123"


def test_parse_row_detail_url_none_when_row_has_no_link():
    row_el = _FakeRowElement(
        "sportsSportsGrid_row_0_asw:event:xyz789",
        ["Dortmund", "Bayern Munich", "Sa. 22.8 • 20:30", "• 124 >>", "4.80", "4.30", "1.62"],
    )
    now_local = datetime(2026, 8, 1, 10, 0, tzinfo=ZURICH)

    row = SwisslosClient._parse_row(row_el, "Bundesliga", "soccer", datetime.now(timezone.utc), now_local)

    assert row is not None
    assert row.detail_url is None


# Real body text captured live 2026-07-24 from a match detail page's
# "Asiatisch" tab (FC Lausanne-Sport vs Grasshopper Club Zurich).
_ASIAN_HANDICAP_SAMPLE = """Asiatisches Handicap
-2 Team 1
5.00
+2 Team 2
1.13
-1 Team 1
2.50
+1 Team 2
1.48
0 Team 1
1.42
0 Team 2
2.65
+1 Team 1
1.10
-1 Team 2
5.90
1. Halbzeit - Asiatisches Handicap
-1 Team 1
4.45
+1 Team 2
1.17
0 Team 1
1.48
0 Team 2
2.45
2. Halbzeit - Asiatisches Handicap
-1 Team 1
3.75
+1 Team 2
1.24
"""


def test_parse_asian_handicap_section_extracts_full_match_lines_only():
    markets = _parse_asian_handicap_section(_ASIAN_HANDICAP_SAMPLE)

    assert set(m[0] for m in markets) == {-2.0, -1.0, 0.0, 1.0}
    by_line = {line: (home_odds, away_odds) for line, home_odds, away_odds in markets}
    assert by_line[-2.0] == (5.00, 1.13)
    assert by_line[0.0] == (1.42, 2.65)


def test_parse_asian_handicap_section_excludes_half_time_variants():
    # 4.45/1.17 and 3.75/1.24 are the 1st/2nd-half odds for line -1/+1 -
    # the full-match line -1 must resolve to the full-match odds (2.50/1.48),
    # not get overwritten by a half-time entry that shares the same line.
    markets = _parse_asian_handicap_section(_ASIAN_HANDICAP_SAMPLE)
    by_line = {line: (home_odds, away_odds) for line, home_odds, away_odds in markets}
    assert by_line[-1.0] == (2.50, 1.48)


def test_parse_asian_handicap_section_missing_heading_returns_empty():
    assert _parse_asian_handicap_section("no handicap markets on this page at all") == []


def test_parse_asian_handicap_section_skips_unpaired_line():
    # a line with only one side present (other side suspended/missing) -
    # must be skipped rather than guessed at.
    text = "Asiatisches Handicap\n-1 Team 1\n2.50\n"
    assert _parse_asian_handicap_section(text) == []
