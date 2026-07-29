"""Betfair Exchange client — Phase 4 step 3 of the audit's remediation
roadmap (2026-07-25): "a second sharp benchmark/exchange including
liquidity and commission" alongside Pinnacle.

Credentials boundary: this module reads BETFAIR_APP_KEY / BETFAIR_USERNAME
/ BETFAIR_PASSWORD from the environment and never hardcodes or logs them.
Creating the Betfair account and obtaining an App Key is something only the
account owner can do (Claude Code will not create accounts or handle
credentials) — this client is ready to run the moment those three env vars
are set, nothing else is blocking it.

API shape confirmed against Betfair's own developer docs and sample code
(2026-07-29):

    Login (interactive):
      POST https://identitysso.betfair.com/api/login
      headers: Accept: application/json, X-Application: <AppKey>,
               Content-Type: application/x-www-form-urlencoded
      body: username=<u>&password=<p>
      -> {"token": <session token>, "product": ..., "status": "SUCCESS"|..., "error": ...}

      Betfair's docs recommend certificate-based ("bot") login for
      unattended automation instead of this interactive flow, since
      interactive sessions expire sooner and are meant for a human being
      present. The cert flow needs an SSL client certificate registered
      with Betfair (a real operational decision — usually also means
      disabling 2FA on the account) that only the account owner can set
      up, so interactive login is what's implemented here; swapping in
      cert-based login later only touches `login()`.

    Betting (JSON-RPC):
      POST https://api.betfair.com/exchange/betting/json-rpc/v1
      headers: X-Application: <AppKey>, X-Authentication: <session token>,
               Content-Type: application/json
      body: {"jsonrpc": "2.0", "method": "SportsAPING/v1.0/<Method>",
             "params": {...}, "id": 1}

      listMarketCatalogue -> [{marketId, marketName, event: {id, name,
        openDate, countryCode}, competition: {name}, runners: [{selectionId,
        runnerName}]}]
      listMarketBook -> [{marketId, runners: [{selectionId, status,
        ex: {availableToBack: [{price, size}, ...], availableToLay: [...]}}]}]
      availableToBack is sorted best price first — [0] is what a bettor
      would actually get matched at right now, and its `size` is the real
      liquidity depth available at that price (not a stated cap the way
      Pinnacle's maxRiskStake is, but the closest honest fit to
      MarketSnapshot.max_bet_size — see fetch note below).

Scope for v1: MATCH_ODDS only (Betfair's 1X2 market -> MarketType.
MATCH_WINNER). Betfair splits Asian handicap and totals into many separate
two-way markets (one market per line) rather than Pinnacle's single
multi-line market, which needs its own line-discovery pass — not modeled
yet, same "not modeled yet" scoping Pinnacle's own client uses for
team_total.

Commission: Betfair charges a percentage of NET WINNINGS on a market, not
on the stake, and only when you win — it doesn't uniformly discount the
quoted price the way a bookmaker's overround does. `effective_odds_after_
commission` folds it into a single net-of-commission decimal-odds figure
(1 + (odds-1)*(1-rate)) so it can be compared against a benchmark's price
using the same edge math as every other book, without touching vb.
fair_probability itself. DEFAULT_COMMISSION_RATE is Betfair's long-standing
published "Standard Discount Rate" in most markets (varies by exact
jurisdiction/account and Betfair may change it — pass commission_rate
explicitly once the account's actual rate is known).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from ..models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection

SITE = "betfair.com"
LOGIN_URL = "https://identitysso.betfair.com/api/login"
BETTING_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"
SOCCER_EVENT_TYPE_ID = "1"  # Betfair's well-known eventTypeId for soccer
DEFAULT_COMMISSION_RATE = 0.05


class BetfairAuthError(Exception):
    pass


def effective_odds_after_commission(back_odds: float, commission_rate: float = DEFAULT_COMMISSION_RATE) -> float:
    """Net-of-commission decimal odds: a winning back bet at `back_odds`
    nets stake*(back_odds-1)*(1-commission_rate) profit, so this is the
    flat decimal price that would produce the same net profit with no
    commission — directly comparable to a bookmaker's quoted price."""
    return 1 + (back_odds - 1) * (1 - commission_rate)


@dataclass
class ScrapedRow:
    event: RawEvent
    snapshots: list[MarketSnapshot]


class BetfairClient:
    def __init__(
        self,
        app_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.app_key = app_key or os.environ.get("BETFAIR_APP_KEY")
        self.username = username or os.environ.get("BETFAIR_USERNAME")
        self.password = password or os.environ.get("BETFAIR_PASSWORD")
        self.timeout = timeout
        self._session_token: Optional[str] = None

    def _require_credentials(self) -> None:
        if not (self.app_key and self.username and self.password):
            raise BetfairAuthError(
                "Betfair credentials missing - set BETFAIR_APP_KEY, BETFAIR_USERNAME and "
                "BETFAIR_PASSWORD env vars (see vb.sources.betfair module docstring)."
            )

    def login(self) -> str:
        self._require_credentials()
        resp = requests.post(
            LOGIN_URL,
            headers={"Accept": "application/json", "X-Application": self.app_key},
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "SUCCESS" or "token" not in data:
            raise BetfairAuthError(f"Betfair login failed: {data.get('error', data.get('status', 'unknown error'))}")
        self._session_token = data["token"]
        return self._session_token

    def _headers(self) -> dict:
        if self._session_token is None:
            self.login()
        return {
            "X-Application": self.app_key,
            "X-Authentication": self._session_token,
            "Content-Type": "application/json",
        }

    def _call(self, method: str, params: dict):
        resp = requests.post(
            BETTING_URL,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": f"SportsAPING/v1.0/{method}", "params": params, "id": 1},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise BetfairAuthError(f"Betfair API error calling {method}: {payload['error']}")
        return payload["result"]

    def list_match_odds_catalogue(self, max_results: int = 200) -> list[dict]:
        """Every MATCH_ODDS (1X2) market currently open for soccer."""
        return self._call(
            "listMarketCatalogue",
            {
                "filter": {"eventTypeIds": [SOCCER_EVENT_TYPE_ID], "marketTypeCodes": ["MATCH_ODDS"]},
                "sort": "FIRST_TO_START",
                "maxResults": str(max_results),
                "marketProjection": ["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME", "COMPETITION"],
            },
        )

    def list_market_books(self, market_ids: list[str]) -> list[dict]:
        """Live prices + liquidity for a batch of market ids (Betfair caps
        this at 40 market ids per call; callers with more should chunk)."""
        if not market_ids:
            return []
        return self._call(
            "listMarketBook",
            {"marketIds": market_ids, "priceProjection": {"priceData": ["EX_BEST_OFFERS"], "virtualise": True}},
        )

    def fetch_soccer_match_odds(self, sport: str = "soccer", batch_size: int = 40) -> list[ScrapedRow]:
        """MATCH_ODDS markets for every soccer event currently listed,
        paired with their live book (best back price + liquidity at that
        price). Markets with no matchable back price on any runner (e.g.
        fully suspended) are omitted."""
        captured_at = datetime.now(timezone.utc)

        catalogue = self.list_match_odds_catalogue()
        if not catalogue:
            return []

        books_by_market: dict[str, dict] = {}
        market_ids = [m["marketId"] for m in catalogue]
        for i in range(0, len(market_ids), batch_size):
            chunk = market_ids[i : i + batch_size]
            for book in self.list_market_books(chunk):
                books_by_market[book["marketId"]] = book

        rows: list[ScrapedRow] = []
        for mkt in catalogue:
            event_info = mkt.get("event") or {}
            name = event_info.get("name") or ""
            if " v " not in name:
                continue  # not a two-team match (e.g. malformed/outright entry)
            home_name, away_name = name.split(" v ", 1)
            open_date = event_info.get("openDate")
            if not open_date:
                continue

            event = RawEvent(
                site=SITE,
                sport=sport,
                competition=(mkt.get("competition") or {}).get("name", ""),
                kickoff_utc=datetime.fromisoformat(open_date.replace("Z", "+00:00")),
                raw_home_team=home_name.strip(),
                raw_away_team=away_name.strip(),
                event_id=str(event_info.get("id") or mkt["marketId"]),
            )

            book = books_by_market.get(mkt["marketId"])
            if book is None:
                continue
            snapshot = self._parse_match_odds(event, mkt, book, captured_at)
            if snapshot is not None:
                rows.append(ScrapedRow(event=event, snapshots=[snapshot]))

        return rows

    @staticmethod
    def _selection_for_runner(runner_name: str, home_name: str, away_name: str) -> Optional[Selection]:
        if runner_name.strip() == home_name.strip():
            return Selection.HOME
        if runner_name.strip() == away_name.strip():
            return Selection.AWAY
        if runner_name.strip().lower() == "the draw":
            return Selection.DRAW
        return None

    @classmethod
    def _parse_match_odds(cls, event: RawEvent, mkt: dict, book: dict, captured_at: datetime) -> Optional[MarketSnapshot]:
        name_by_selection = {r["selectionId"]: r.get("runnerName", "") for r in mkt.get("runners") or []}
        best_liquidity: Optional[float] = None
        outcomes: list[Outcome] = []
        for runner in book.get("runners") or []:
            if runner.get("status") != "ACTIVE":
                continue
            runner_name = name_by_selection.get(runner.get("selectionId"))
            if runner_name is None:
                continue
            selection = cls._selection_for_runner(runner_name, event.raw_home_team, event.raw_away_team)
            if selection is None:
                continue
            back_prices = ((runner.get("ex") or {}).get("availableToBack")) or []
            if not back_prices:
                continue
            best = back_prices[0]
            outcomes.append(Outcome(selection, best["price"]))
            size = best.get("size")
            if size is not None:
                best_liquidity = size if best_liquidity is None else min(best_liquidity, size)

        if len(outcomes) != 3:
            return None
        return MarketSnapshot(
            event=event, market_type=MarketType.MATCH_WINNER, line=None,
            outcomes=tuple(outcomes), captured_at=captured_at, max_bet_size=best_liquidity,
        )
