"""Swisslos Sporttip scraper (headless Playwright).

Reads the same rendered odds grid a visitor sees at
swisslos.ch/de/sporttip/sportwetten/fussball — not an internal API call.
Swisslos's real data feed is routed through an obfuscated, token-per-session
same-origin endpoint that looks purpose-built to resist scripted access.
Rendering the actual page with a real browser engine sidesteps that without
trying to reverse-engineer or defeat that protection: this reads exactly
what any visitor's browser displays.

Grid DOM shape confirmed by hand (2026-07-23), Admiral-platform "Sporttip"
grid (asw-sports-grid-* custom elements):

    asw-sports-grid-expandable        (one per league/competition)
      > div                            league header text, e.g.
                                        "Qualifikation, Europa League1  X  2  Over  Tore  Under"
      > [id="sportsSportsGrid_row_{n}_asw:event:{id}"]   (one per match)
          10 leaf text nodes, in order:
            home_team, away_team, "<reltime> • HH:MM", "• N >>",
            odds_home, odds_draw, odds_away, odds_over, total_line, odds_under

Only match-winner (1X2) and totals show in this default grid view — the
column set is user-configurable on the live site (dropdowns can swap in
"Handicap" etc) but this scraper reads whatever the default view shows.
Handicap capture is a TODO (see methodology's "match not only match-winner
but handicaps etc").

No virtualization was observed: a full section's rows are all present in
the DOM after load, no scroll-triggered lazy loading to handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from ..models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection

SITE = "swisslos.ch"
# The "Alle Bewerbe" (all competitions) tab, not the default "Top Bewerbe"
# view - roughly doubles coverage (confirmed live 2026-07-23: ~20 events
# vs ~10-17 on the default view). This still isn't the full ~517 the
# sidebar count claims for the whole football section; reaching that
# would mean iterating every competition individually (comparable in
# scope to Pinnacle's fetch_all_soccer() league sweep) - not done here.
FOOTBALL_URL = "https://www.swisslos.ch/de/sporttip/sportwetten/fussball?viewConfig=id:asw:viewconfig:100"

_ROW_ID_RE = re.compile(r"^sportsSportsGrid_row_\d+_asw:event:([0-9a-zA-Z]+)$")
_ZURICH = ZoneInfo("Europe/Zurich")


_EXPLICIT_DATE_RE = re.compile(r"^(?:[a-zé]{2,3}\.\s*)?(\d{1,2})\.(\d{1,2})\.?$", re.IGNORECASE)


def _parse_kickoff(rel_text: str, now_local: datetime) -> Optional[datetime]:
    """Parse Swisslos's relative date strings into a UTC datetime.
    `now_local` anchors what "Heute"/"Morgen" mean and must already be in
    Europe/Zurich time. Two date formats have been observed: plain
    "25.07" on the default "Top Bewerbe" view, and a weekday-prefixed,
    non-zero-padded "Sa. 25.7" on the "Alle Bewerbe" view (further-out
    fixtures) - both matched by _EXPLICIT_DATE_RE, the weekday prefix is
    just discarded since day+month alone is enough to resolve the date.
    """
    m = re.match(r"(.+?)\s*[•·]\s*(\d{1,2}):(\d{2})", rel_text)
    if not m:
        return None
    day_part, hour, minute = m.group(1).strip().lower(), int(m.group(2)), int(m.group(3))

    if day_part == "heute":
        local_date = now_local.date()
    elif day_part == "morgen":
        local_date = (now_local + timedelta(days=1)).date()
    else:
        dm = _EXPLICIT_DATE_RE.match(day_part)
        if not dm:
            return None  # unrecognized format - not seen in samples yet
        day, month = int(dm.group(1)), int(dm.group(2))
        year = now_local.year
        candidate = now_local.replace(year=year, month=month, day=day)
        if candidate.date() < (now_local - timedelta(days=1)).date():
            year += 1  # date already passed this year -> must be next year's occurrence
        local_date = now_local.replace(year=year, month=month, day=day).date()

    local_dt = datetime(local_date.year, local_date.month, local_date.day, hour, minute, tzinfo=_ZURICH)
    return local_dt.astimezone(timezone.utc)


def _competition_name(header_text: str) -> str:
    # Header text glues the market-column labels onto the end, e.g.
    # "Qualifikation, Europa League1  X  2  Over  Tore  Under" - keep only
    # what comes before the first digit.
    m = re.match(r"^[^\d]*", header_text)
    return (m.group(0) if m else header_text).strip()


@dataclass
class ScrapedRow:
    event: RawEvent
    snapshots: list[MarketSnapshot]


class SwisslosClient:
    def __init__(self, headless: bool = True, timeout_ms: int = 30000):
        self.headless = headless
        self.timeout_ms = timeout_ms

    def fetch_football(self, sport: str = "soccer") -> list[ScrapedRow]:
        captured_at = datetime.now(timezone.utc)
        now_local = datetime.now(_ZURICH)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                page = browser.new_page()
                page.goto(FOOTBALL_URL, wait_until="load", timeout=self.timeout_ms)
                page.wait_for_selector('[id^="sportsSportsGrid_row_"]', timeout=self.timeout_ms)
                page.wait_for_timeout(1500)

                results: list[ScrapedRow] = []
                for group in page.query_selector_all("asw-sports-grid-expandable"):
                    header_el = group.query_selector(":scope > div")
                    competition = _competition_name(header_el.inner_text() if header_el else "")

                    for row_el in group.query_selector_all('[id^="sportsSportsGrid_row_"]'):
                        row = self._parse_row(row_el, competition, sport, captured_at, now_local)
                        if row is not None:
                            results.append(row)
                return results
            finally:
                browser.close()

    @staticmethod
    def _parse_row(row_el, competition: str, sport: str, captured_at: datetime, now_local: datetime) -> Optional["ScrapedRow"]:
        row_id = row_el.get_attribute("id") or ""
        m = _ROW_ID_RE.match(row_id)
        if not m:
            return None  # a sub-element (icon, tap-area, ...), not a top-level row

        texts = row_el.eval_on_selector_all(
            "*",
            "els => els.filter(e => e.children.length === 0 && "
            "e.textContent.trim()).map(e => e.textContent.trim())",
        )
        if len(texts) < 10:
            return None  # incomplete row (market not offered / not yet rendered) - skip rather than guess

        home, away, rel_time = texts[0], texts[1], texts[2]
        kickoff = _parse_kickoff(rel_time, now_local)
        if kickoff is None:
            return None

        event = RawEvent(
            site=SITE,
            sport=sport,
            competition=competition,
            kickoff_utc=kickoff,
            raw_home_team=home,
            raw_away_team=away,
            event_id=m.group(1),
        )

        snapshots: list[MarketSnapshot] = []
        try:
            odds_home, odds_draw, odds_away = float(texts[4]), float(texts[5]), float(texts[6])
            snapshots.append(
                MarketSnapshot(
                    event=event,
                    market_type=MarketType.MATCH_WINNER,
                    line=None,
                    outcomes=(
                        Outcome(Selection.HOME, odds_home),
                        Outcome(Selection.DRAW, odds_draw),
                        Outcome(Selection.AWAY, odds_away),
                    ),
                    captured_at=captured_at,
                )
            )
        except ValueError:
            pass  # not parseable as odds (e.g. market suspended/placeholder) - skip that market, keep the event

        try:
            odds_over, total_line, odds_under = float(texts[7]), float(texts[8]), float(texts[9])
            snapshots.append(
                MarketSnapshot(
                    event=event,
                    market_type=MarketType.TOTALS,
                    line=total_line,
                    outcomes=(
                        Outcome(Selection.OVER, odds_over),
                        Outcome(Selection.UNDER, odds_under),
                    ),
                    captured_at=captured_at,
                )
            )
        except ValueError:
            pass

        if not snapshots:
            return None
        return ScrapedRow(event=event, snapshots=snapshots)
