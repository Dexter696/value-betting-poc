from datetime import datetime, timezone

import pytest

from vb.models import MarketType, Selection
from vb.sources.betfair import BetfairAuthError, BetfairClient, effective_odds_after_commission

CAPTURED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

CATALOGUE_ENTRY = {
    "marketId": "1.127771425",
    "marketName": "Match Odds",
    "event": {"id": "31200000", "name": "Man City v Bournemouth", "openDate": "2026-08-01T14:00:00.000Z"},
    "competition": {"name": "English Premier League"},
    "runners": [
        {"selectionId": 1, "runnerName": "Man City"},
        {"selectionId": 2, "runnerName": "Bournemouth"},
        {"selectionId": 3, "runnerName": "The Draw"},
    ],
}

BOOK_ENTRY = {
    "marketId": "1.127771425",
    "runners": [
        {"selectionId": 1, "status": "ACTIVE", "ex": {"availableToBack": [{"price": 1.5, "size": 120.0}]}},
        {"selectionId": 2, "status": "ACTIVE", "ex": {"availableToBack": [{"price": 6.4, "size": 80.0}]}},
        {"selectionId": 3, "status": "ACTIVE", "ex": {"availableToBack": [{"price": 4.5, "size": 200.0}]}},
    ],
}


def test_effective_odds_after_commission_discounts_only_the_profit_portion():
    # back odds 2.0 on a 1-unit stake nets 1.0 profit; 5% commission on
    # that profit -> 0.95 net profit -> effective odds 1.95, not 1.90
    assert effective_odds_after_commission(2.0, commission_rate=0.05) == pytest.approx(1.95)


def test_effective_odds_after_commission_zero_rate_is_a_no_op():
    assert effective_odds_after_commission(3.2, commission_rate=0.0) == pytest.approx(3.2)


def test_login_requires_credentials():
    client = BetfairClient(app_key=None, username=None, password=None)
    with pytest.raises(BetfairAuthError):
        client.login()


def test_login_raises_on_failure_status(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "INVALID_CREDENTIALS", "error": "INVALID_CREDENTIALS"}

    monkeypatch.setattr("vb.sources.betfair.requests.post", lambda *a, **k: FakeResp())
    with pytest.raises(BetfairAuthError):
        client.login()


def test_login_stores_session_token(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "SUCCESS", "token": "sess-123"}

    monkeypatch.setattr("vb.sources.betfair.requests.post", lambda *a, **k: FakeResp())
    token = client.login()
    assert token == "sess-123"
    assert client._session_token == "sess-123"


def test_selection_for_runner_matches_home_away_and_draw():
    assert BetfairClient._selection_for_runner("Man City", "Man City", "Bournemouth") == Selection.HOME
    assert BetfairClient._selection_for_runner("Bournemouth", "Man City", "Bournemouth") == Selection.AWAY
    assert BetfairClient._selection_for_runner("The Draw", "Man City", "Bournemouth") == Selection.DRAW
    assert BetfairClient._selection_for_runner("Some Other Team", "Man City", "Bournemouth") is None


def test_fetch_soccer_match_odds_parses_catalogue_and_book(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")
    monkeypatch.setattr(client, "list_match_odds_catalogue", lambda: [CATALOGUE_ENTRY])
    monkeypatch.setattr(client, "list_market_books", lambda market_ids: [BOOK_ENTRY])

    rows = client.fetch_soccer_match_odds()

    assert len(rows) == 1
    row = rows[0]
    assert row.event.raw_home_team == "Man City"
    assert row.event.raw_away_team == "Bournemouth"
    assert row.event.event_id == "31200000"
    assert row.event.competition == "English Premier League"

    snapshot = row.snapshots[0]
    assert snapshot.market_type == MarketType.MATCH_WINNER
    assert snapshot.line is None
    odds = {o.selection: o.odds for o in snapshot.outcomes}
    assert odds[Selection.HOME] == 1.5
    assert odds[Selection.AWAY] == 6.4
    assert odds[Selection.DRAW] == 4.5
    assert snapshot.max_bet_size == 80.0  # min liquidity across the three best prices


def test_fetch_soccer_match_odds_skips_market_with_no_book(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")
    monkeypatch.setattr(client, "list_match_odds_catalogue", lambda: [CATALOGUE_ENTRY])
    monkeypatch.setattr(client, "list_market_books", lambda market_ids: [])

    assert client.fetch_soccer_match_odds() == []


def test_fetch_soccer_match_odds_skips_runner_with_no_back_price(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")
    monkeypatch.setattr(client, "list_match_odds_catalogue", lambda: [CATALOGUE_ENTRY])
    thin_book = {
        "marketId": "1.127771425",
        "runners": [
            {"selectionId": 1, "status": "ACTIVE", "ex": {"availableToBack": [{"price": 1.5, "size": 120.0}]}},
            {"selectionId": 2, "status": "ACTIVE", "ex": {"availableToBack": []}},
            {"selectionId": 3, "status": "ACTIVE", "ex": {"availableToBack": [{"price": 4.5, "size": 200.0}]}},
        ],
    }
    monkeypatch.setattr(client, "list_market_books", lambda market_ids: [thin_book])

    assert client.fetch_soccer_match_odds() == []  # only 2 of 3 outcomes parseable -> dropped


def test_fetch_soccer_match_odds_skips_event_without_v_separator(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")
    bad_entry = dict(CATALOGUE_ENTRY, event={"id": "31200000", "name": "Outright Winner Market", "openDate": "2026-08-01T14:00:00.000Z"})
    monkeypatch.setattr(client, "list_match_odds_catalogue", lambda: [bad_entry])
    monkeypatch.setattr(client, "list_market_books", lambda market_ids: [BOOK_ENTRY])

    assert client.fetch_soccer_match_odds() == []


def test_call_raises_on_a_non_session_api_error(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")
    client._session_token = "sess-123"

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": {"errorCode": "INVALID_INPUT_DATA"}}

    monkeypatch.setattr("vb.sources.betfair.requests.post", lambda *a, **k: FakeResp())
    with pytest.raises(BetfairAuthError):
        client.list_match_odds_catalogue()


def test_call_retries_once_on_expired_session_and_succeeds(monkeypatch):
    # Regression found by self-review (2026-07-29): a session expiring
    # mid-scrape (e.g. between chunked listMarketBook calls) used to
    # abort the whole scrape instead of re-authenticating once.
    client = BetfairClient(app_key="k", username="u", password="p")
    client._session_token = "stale-token"
    calls = {"n": 0}

    class SessionExpiredResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": {"errorCode": "INVALID_SESSION_INFORMATION"}}

    class LoginSuccessResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "SUCCESS", "token": "fresh-token"}

    class CatalogueSuccessResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": [CATALOGUE_ENTRY]}

    def fake_post(url, *a, **k):
        calls["n"] += 1
        if url == "https://identitysso.betfair.com/api/login":
            return LoginSuccessResp()
        if calls["n"] == 1:
            return SessionExpiredResp()
        return CatalogueSuccessResp()

    monkeypatch.setattr("vb.sources.betfair.requests.post", fake_post)

    result = client.list_match_odds_catalogue()

    assert result == [CATALOGUE_ENTRY]
    assert client._session_token == "fresh-token"


def test_call_raises_if_session_error_persists_after_retry(monkeypatch):
    client = BetfairClient(app_key="k", username="u", password="p")
    client._session_token = "stale-token"

    class SessionExpiredResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": {"errorCode": "INVALID_SESSION_INFORMATION"}}

    class LoginSuccessResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "SUCCESS", "token": "fresh-token"}

    def fake_post(url, *a, **k):
        if url == "https://identitysso.betfair.com/api/login":
            return LoginSuccessResp()
        return SessionExpiredResp()  # keeps failing even after a fresh login

    monkeypatch.setattr("vb.sources.betfair.requests.post", fake_post)

    with pytest.raises(BetfairAuthError):
        client.list_match_odds_catalogue()
