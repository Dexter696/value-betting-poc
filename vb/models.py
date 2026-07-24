"""Core data model for odds snapshots, shared by every site's scraper.

A RawEvent is a match as reported by one site. A MarketSnapshot is one
market (match winner / handicap / totals) on one event on one site,
captured at a point in time. Everything downstream (matching, edge
calculation) is built on these two shapes so scrapers only need to
translate site-specific payloads into them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class MarketType(str, Enum):
    MATCH_WINNER = "match_winner"      # 1X2
    ASIAN_HANDICAP = "asian_handicap"
    TOTALS = "totals"                  # over/under


class Selection(str, Enum):
    HOME = "home"
    AWAY = "away"
    DRAW = "draw"
    OVER = "over"
    UNDER = "under"


@dataclass(frozen=True)
class RawEvent:
    """A match as reported by one site, before cross-site matching."""

    site: str
    sport: str
    competition: str
    kickoff_utc: datetime
    raw_home_team: str
    raw_away_team: str
    event_id: str  # site's own identifier, kept for traceability/debugging


@dataclass(frozen=True)
class Outcome:
    selection: Selection
    odds: float


@dataclass(frozen=True)
class MarketSnapshot:
    """One market on one event on one site, at one point in time.

    `line` is None for match_winner. For asian_handicap it is always
    expressed from the HOME team's perspective (negative = home favored)
    regardless of how the source site quoted it — see
    normalize.canonical_handicap_line. For totals it is the total itself
    (e.g. 2.5).
    """

    event: RawEvent
    market_type: MarketType
    line: Optional[float]
    outcomes: tuple[Outcome, ...]
    captured_at: datetime
    max_bet_size: Optional[float] = None  # site's stated cap on this market, when exposed
