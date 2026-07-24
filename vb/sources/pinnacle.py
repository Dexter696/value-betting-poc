"""Pinnacle client (guest API, no account needed).

Reads the same internal "guest" API Pinnacle's own website calls to render
odds (guest.api.arcadia.pinnacle.com). This is not an authenticated feed:
the API key below is not a personal credential — it's the anonymous key
Pinnacle's frontend sends from every visitor's browser, and the same
approach used by several public odds-scraping projects (e.g.
github.com/pretrehr/Sports-betting). It intermittently 503s
("MAINTENANCE") and may rotate — if it starts failing consistently, pull a
fresh key from a real browser's Network tab (any `.../markets/straight`
request's `X-API-Key` request header) and pass it via `api_key=` or the
PINNACLE_API_KEY env var.

Endpoint shapes confirmed live 2026-07-23 against league 1980 (England -
Premier League):

    GET /leagues/{id}/matchups
      -> list of events. Each has: id, type ("matchup" vs "special" -
         specials are outrights/futures, e.g. "League Winner", and are
         skipped), participants: [{alignment: "home"/"away", name}],
         startTime (ISO8601 UTC), league.name, league.sport.name.

    GET /leagues/{id}/markets/straight
      -> list of markets. Each has: matchupId (links to a matchup id
         above), period (0 = full match; other values are 1st/2nd half
         etc, skipped for v1), type (moneyline/spread/total/team_total -
         only the first three map to our MarketType; team_total is a
         side's own goal total, a different market, not modeled yet),
         prices: [{designation, price (American odds), points (spread/
         total line; absent on moneyline)}], limits:
         [{type: "maxRiskStake", amount}] -> our max_bet_size.

American odds -> decimal: positive -> odds/100 + 1; negative -> 100/abs(odds) + 1.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from ..models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection

SITE = "pinnacle.com"
BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
DEFAULT_API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"  # public guest key - see module docstring
SOCCER_SPORT_ID = 29

_MARKET_TYPE_MAP = {
    "moneyline": MarketType.MATCH_WINNER,
    "spread": MarketType.ASIAN_HANDICAP,
    "total": MarketType.TOTALS,
}


def _decimal_odds(american: float) -> float:
    if american > 0:
        return round(american / 100 + 1, 4)
    return round(-100 / american + 1, 4)


def _max_bet_size(limits: list) -> Optional[float]:
    for lim in limits or []:
        if lim.get("type") == "maxRiskStake":
            return lim.get("amount")
    return None


@dataclass
class ScrapedRow:
    event: RawEvent
    snapshots: list[MarketSnapshot]


class PinnacleClient:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("PINNACLE_API_KEY", DEFAULT_API_KEY)
        self.timeout = timeout

    def _get(self, path: str):
        resp = requests.get(
            f"{BASE_URL}{path}",
            headers={"X-API-Key": self.api_key, "Referer": "https://www.pinnacle.com/"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_league(self, league_id: int, sport: str = "soccer") -> list[ScrapedRow]:
        """Fetch matchups + straight markets for one league and pair them
        into events with their market snapshots. Events with no parseable
        markets (e.g. all markets suspended) are omitted rather than
        returned empty-handed.
        """
        captured_at = datetime.now(timezone.utc)

        events: dict[int, RawEvent] = {}
        for m in self._get(f"/leagues/{league_id}/matchups"):
            if m.get("type") != "matchup":
                continue  # skip outrights/futures ("special")
            participants = m.get("participants") or []
            home = next((p["name"] for p in participants if p.get("alignment") == "home"), None)
            away = next((p["name"] for p in participants if p.get("alignment") == "away"), None)
            start_time = m.get("startTime")
            if not home or not away or not start_time:
                continue
            events[m["id"]] = RawEvent(
                site=SITE,
                sport=sport,
                competition=(m.get("league") or {}).get("name", ""),
                kickoff_utc=datetime.fromisoformat(start_time.replace("Z", "+00:00")),
                raw_home_team=home,
                raw_away_team=away,
                event_id=str(m["id"]),
            )

        snapshots_by_event: dict[int, list[MarketSnapshot]] = {}
        for mkt in self._get(f"/leagues/{league_id}/markets/straight"):
            if mkt.get("period") != 0:
                continue  # full match only for v1 (skip 1st/2nd half etc)
            market_type = _MARKET_TYPE_MAP.get(mkt.get("type"))
            if market_type is None:
                continue  # e.g. team_total - not modeled yet
            event = events.get(mkt.get("matchupId"))
            if event is None:
                continue
            snapshot = self._parse_market(event, market_type, mkt, captured_at)
            if snapshot is not None:
                snapshots_by_event.setdefault(mkt["matchupId"], []).append(snapshot)

        return [
            ScrapedRow(event=event, snapshots=snapshots_by_event[matchup_id])
            for matchup_id, event in events.items()
            if matchup_id in snapshots_by_event
        ]

    def fetch_soccer_league_ids(self) -> list[int]:
        """Every soccer league id currently offering at least one match
        (confirmed live 2026-07-23: 113 leagues, 656 matches total)."""
        leagues = self._get(f"/sports/{SOCCER_SPORT_ID}/leagues")
        return [l["id"] for l in leagues if l.get("matchupCount", 0) > 0]

    def fetch_all_soccer(self, request_delay: float = 0.15) -> list[ScrapedRow]:
        """Fetch every soccer league currently offering matches. A small
        delay between leagues keeps this from bursting Pinnacle's guest
        API back to back across ~110+ leagues — it's meant for a single
        visitor's browser, not a client sweeping the whole sport. One
        league failing (e.g. a transient error) doesn't abort the sweep;
        it's just skipped.
        """
        rows: list[ScrapedRow] = []
        league_ids = self.fetch_soccer_league_ids()
        for i, league_id in enumerate(league_ids):
            try:
                rows.extend(self.fetch_league(league_id))
            except requests.RequestException:
                pass
            if i < len(league_ids) - 1:
                time.sleep(request_delay)
        return rows

    @staticmethod
    def _parse_market(
        event: RawEvent, market_type: MarketType, mkt: dict, captured_at: datetime
    ) -> Optional[MarketSnapshot]:
        prices = mkt.get("prices") or []
        max_bet_size = _max_bet_size(mkt.get("limits"))

        if market_type == MarketType.MATCH_WINNER:
            selection_map = {"home": Selection.HOME, "away": Selection.AWAY, "draw": Selection.DRAW}
            outcomes = [
                Outcome(selection_map[p["designation"]], _decimal_odds(p["price"]))
                for p in prices
                if p.get("designation") in selection_map and "price" in p
            ]
            if len(outcomes) != 3:
                return None
            return MarketSnapshot(
                event=event, market_type=market_type, line=None,
                outcomes=tuple(outcomes), captured_at=captured_at, max_bet_size=max_bet_size,
            )

        if market_type == MarketType.ASIAN_HANDICAP:
            outcomes = []
            line = None
            for p in prices:
                designation = p.get("designation")
                if designation not in ("home", "away") or "price" not in p or "points" not in p:
                    continue
                selection = Selection.HOME if designation == "home" else Selection.AWAY
                outcomes.append(Outcome(selection, _decimal_odds(p["price"])))
                if designation == "home":
                    # Pinnacle already quotes each side's own points value,
                    # so home's own points IS the home-perspective line -
                    # no sign flip needed (contrast with normalize.
                    # canonical_handicap_line, needed when a site only
                    # exposes the away side's points).
                    line = p["points"]
            if len(outcomes) != 2 or line is None:
                return None
            return MarketSnapshot(
                event=event, market_type=market_type, line=line,
                outcomes=tuple(outcomes), captured_at=captured_at, max_bet_size=max_bet_size,
            )

        if market_type == MarketType.TOTALS:
            selection_map = {"over": Selection.OVER, "under": Selection.UNDER}
            outcomes = []
            line = None
            for p in prices:
                if p.get("designation") not in selection_map or "price" not in p or "points" not in p:
                    continue
                outcomes.append(Outcome(selection_map[p["designation"]], _decimal_odds(p["price"])))
                line = p["points"]
            if len(outcomes) != 2 or line is None:
                return None
            return MarketSnapshot(
                event=event, market_type=market_type, line=line,
                outcomes=tuple(outcomes), captured_at=captured_at, max_bet_size=max_bet_size,
            )

        return None
