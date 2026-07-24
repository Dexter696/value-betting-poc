from datetime import datetime, timezone

import requests

from vb.models import MarketType, RawEvent, Selection
from vb.sources.pinnacle import PinnacleClient, _decimal_odds, _max_bet_size

CAPTURED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _event():
    return RawEvent(
        site="pinnacle.com", sport="soccer", competition="England - Premier League",
        kickoff_utc=CAPTURED_AT, raw_home_team="Man City", raw_away_team="Bournemouth",
        event_id="1632011616",
    )


def test_decimal_odds_positive_american():
    assert _decimal_odds(588) == 6.88


def test_decimal_odds_negative_american():
    assert round(_decimal_odds(-223), 3) == 1.448


def test_max_bet_size_extracts_maxRiskStake():
    limits = [{"amount": 500, "type": "maxRiskStake"}]
    assert _max_bet_size(limits) == 500


def test_max_bet_size_missing_returns_none():
    assert _max_bet_size([]) is None
    assert _max_bet_size(None) is None


def test_parse_moneyline_market():
    mkt = {
        "matchupId": 1632011616, "period": 0, "type": "moneyline",
        "prices": [
            {"designation": "home", "price": 588},
            {"designation": "away", "price": -223},
            {"designation": "draw", "price": 327},
        ],
        "limits": [{"amount": 500, "type": "maxRiskStake"}],
    }
    snapshot = PinnacleClient._parse_market(_event(), MarketType.MATCH_WINNER, mkt, CAPTURED_AT)

    assert snapshot is not None
    assert snapshot.line is None
    assert snapshot.max_bet_size == 500
    odds = {o.selection: o.odds for o in snapshot.outcomes}
    assert odds[Selection.HOME] == 6.88
    assert odds[Selection.DRAW] == 4.27
    assert round(odds[Selection.AWAY], 3) == 1.448


def test_parse_spread_market_uses_home_points_directly():
    # Pinnacle quotes each side's own points; home's own points value IS
    # already the home-perspective line, even when home is the underdog
    # (positive points), unlike the canonical_handicap_line() flip needed
    # when only the away side is exposed by a site.
    mkt = {
        "matchupId": 1632011616, "period": 0, "type": "spread",
        "prices": [
            {"designation": "home", "points": 1.25, "price": -122},
            {"designation": "away", "points": -1.25, "price": 101},
        ],
        "limits": [{"amount": 500, "type": "maxRiskStake"}],
    }
    snapshot = PinnacleClient._parse_market(_event(), MarketType.ASIAN_HANDICAP, mkt, CAPTURED_AT)

    assert snapshot is not None
    assert snapshot.line == 1.25


def test_parse_total_market():
    mkt = {
        "matchupId": 1632011616, "period": 0, "type": "total",
        "prices": [
            {"designation": "over", "points": 2.75, "price": -107},
            {"designation": "under", "points": 2.75, "price": -115},
        ],
        "limits": [{"amount": 500, "type": "maxRiskStake"}],
    }
    snapshot = PinnacleClient._parse_market(_event(), MarketType.TOTALS, mkt, CAPTURED_AT)

    assert snapshot is not None
    assert snapshot.line == 2.75
    odds = {o.selection: o.odds for o in snapshot.outcomes}
    assert round(odds[Selection.OVER], 3) == round(_decimal_odds(-107), 3)


def test_parse_market_returns_none_on_incomplete_prices():
    mkt = {
        "matchupId": 1632011616, "period": 0, "type": "moneyline",
        "prices": [{"designation": "home", "price": 588}],  # missing away/draw
        "limits": [],
    }
    assert PinnacleClient._parse_market(_event(), MarketType.MATCH_WINNER, mkt, CAPTURED_AT) is None


def test_fetch_soccer_league_ids_filters_empty_leagues(monkeypatch):
    client = PinnacleClient()
    monkeypatch.setattr(
        client, "_get",
        lambda path: [
            {"id": 1980, "name": "England - Premier League", "matchupCount": 11},
            {"id": 9999, "name": "Empty League", "matchupCount": 0},
            {"id": 2436, "name": "Italy - Serie A", "matchupCount": 5},
        ],
    )
    assert client.fetch_soccer_league_ids() == [1980, 2436]


def test_fetch_all_soccer_aggregates_across_leagues(monkeypatch):
    client = PinnacleClient()
    monkeypatch.setattr(client, "fetch_soccer_league_ids", lambda: [1, 2])
    monkeypatch.setattr(client, "fetch_league", lambda league_id, sport="soccer": [f"row-from-{league_id}"])
    monkeypatch.setattr("vb.sources.pinnacle.time.sleep", lambda seconds: None)

    rows = client.fetch_all_soccer()

    assert rows == ["row-from-1", "row-from-2"]


def test_fetch_all_soccer_skips_a_league_that_errors(monkeypatch):
    client = PinnacleClient()
    monkeypatch.setattr(client, "fetch_soccer_league_ids", lambda: [1, 2, 3])
    monkeypatch.setattr("vb.sources.pinnacle.time.sleep", lambda seconds: None)

    def fake_fetch_league(league_id, sport="soccer"):
        if league_id == 2:
            raise requests.HTTPError("503 for league 2")
        return [f"row-from-{league_id}"]

    monkeypatch.setattr(client, "fetch_league", fake_fetch_league)

    rows = client.fetch_all_soccer()

    assert rows == ["row-from-1", "row-from-3"]  # league 2 skipped, sweep continued
