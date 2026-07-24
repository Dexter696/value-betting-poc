from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from vb.models import MarketType
from vb.sources.swisslos import MAX_STAKE_CHF, SwisslosClient, _competition_name, _parse_kickoff

ZURICH = ZoneInfo("Europe/Zurich")


class _FakeRowElement:
    """Stands in for a Playwright ElementHandle for _parse_row tests -
    only the two methods _parse_row actually calls."""

    def __init__(self, row_id: str, texts: list[str]):
        self._row_id = row_id
        self._texts = texts

    def get_attribute(self, name):
        assert name == "id"
        return self._row_id

    def eval_on_selector_all(self, selector, script):
        return self._texts


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
