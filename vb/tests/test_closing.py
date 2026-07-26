from datetime import datetime, timezone

import math

import pytest

from vb.closing import SourceClosingPrice, closing_line_value, consensus_closing_odds, record_closing_snapshot
from vb.identity import new_id
from vb.models import CanonicalEvent, MarketType, Selection
from vb.storage import init_db, save_canonical_event

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def test_consensus_closing_odds_is_the_median_not_the_mean():
    prices = [SourceClosingPrice("a", 2.00), SourceClosingPrice("b", 2.10), SourceClosingPrice("c", 5.00)]
    # median of [2.00, 2.10, 5.00] is 2.10 - the mean (~3.03) would be
    # dragged far off by the one outlier
    assert consensus_closing_odds(prices) == 2.10


def test_consensus_closing_odds_averages_the_middle_two_for_an_even_count():
    prices = [SourceClosingPrice("a", 2.00), SourceClosingPrice("b", 2.20)]
    assert consensus_closing_odds(prices) == 2.10


def test_consensus_closing_odds_requires_at_least_one_price():
    with pytest.raises(ValueError):
        consensus_closing_odds([])


def test_closing_line_value_is_positive_when_accepted_price_was_better():
    assert closing_line_value(accepted_odds=2.30, consensus_odds=2.10) > 0


def test_closing_line_value_is_negative_when_accepted_price_was_worse():
    assert closing_line_value(accepted_odds=2.00, consensus_odds=2.10) < 0


def test_closing_line_value_is_zero_when_accepted_price_matches_consensus():
    assert closing_line_value(accepted_odds=2.10, consensus_odds=2.10) == 0.0


def test_record_closing_snapshot_persists_the_consensus_and_every_source(tmp_path):
    conn = _db(tmp_path)
    canonical_id = new_id()
    save_canonical_event(conn, CanonicalEvent(id=canonical_id, sport="soccer", created_at=T0))

    prices = [SourceClosingPrice("swisslos.ch", 2.10), SourceClosingPrice("loro.ch", 2.20)]
    snapshot_id = record_closing_snapshot(
        conn, canonical_id, MarketType.MATCH_WINNER, None, Selection.HOME, T0, prices,
    )

    row = conn.execute(
        "SELECT canonical_event_id, consensus_odds, source_json FROM closing_snapshot WHERE id = ?", (snapshot_id,)
    ).fetchone()
    assert row[0] == canonical_id
    assert math.isclose(row[1], 2.15, abs_tol=1e-9)  # median of two -> average of both
    assert "swisslos.ch" in row[2] and "loro.ch" in row[2]
