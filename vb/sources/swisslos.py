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

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from ..models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection

SITE = "swisslos.ch"
# The "Alle Bewerbe" (all competitions) tab, not the default "Top Bewerbe"
# view - roughly doubles coverage vs the default view. Used by
# fetch_football() for a quick single-page fetch; fetch_all_countries()
# below is what actually reaches full breadth.
FOOTBALL_URL = "https://www.swisslos.ch/de/sporttip/sportwetten/fussball?viewConfig=id:asw:viewconfig:100"

# Every country/competition-group slug Swisslos's own country picker
# lists for football (confirmed live 2026-07-24 by opening the picker and
# scrolling it to force all entries to render, then reading each link's
# href - see project notes). Each is its own page at
# swisslos.ch/de/sporttip/sportwetten/fussball/{slug} showing that
# country/competition's full match list directly (no lazy-loading tricks
# needed there, unlike the aggregate "Alle Bewerbe" view). This list is
# static, not re-derived at runtime, so it'll silently miss any
# competition Swisslos adds later - re-derive it the same way if
# coverage looks stale.
SOCCER_COUNTRY_SLUGS = [
    "schweiz", "klub-freundschaftsspiele", "polen", "rumaenien", "daenemark",
    "schweden", "island", "norwegen", "argentinien", "brasilien", "usa",
    "england", "spanien", "italien", "deutschland", "champions-league",
    "europa-league", "conference-league", "wm-2030", "belgien", "bolivien",
    "bulgarien", "chile", "china", "em-2028", "frankreich", "griechenland",
    "international-rest", "kroatien", "mexiko", "niederlande", "oesterreich",
    "portugal", "schottland", "slowakei", "slowenien", "suedkorea",
    "tschechien", "tuerkei", "ungarn", "uruguay", "wales",
]

# Swisslos doesn't publish a per-market max stake like Pinnacle's
# maxRiskStake - it's a flat, site-wide, per-single-bet cap, confirmed
# live (2026-07-24) via the anonymous (no login needed) client-side
# validation message that appears when a stake above this is entered in
# the bet slip: "Dein maximaler Einsatz betraegt CHF 1'000.00." Same
# figure reproduced across multiple different matches/markets, so this
# is recorded as a constant rather than scraped per-row. Re-verify the
# same way if it ever looks stale (Swisslos could change the policy).
MAX_STAKE_CHF = 1000.0

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
        """Quick single-page fetch of the "Alle Bewerbe" aggregate view.
        Faster than fetch_all_countries() but only ever shows the subset of
        competitions that view surfaces - use fetch_all_countries() for
        full breadth.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                page = browser.new_page()
                return self._fetch_page(page, FOOTBALL_URL, sport)
            finally:
                browser.close()

    def fetch_all_countries(self, sport: str = "soccer") -> list[ScrapedRow]:
        """Full-breadth fetch: visits every page in SOCCER_COUNTRY_SLUGS in
        one browser session (reusing a single page/tab, not relaunching
        Chromium per country) and aggregates the results, deduping by
        event_id since international competitions (Champions League etc)
        can appear under more than one country grouping.

        Measured ~290s for all 42 pages live. Concurrent tabs (async
        Playwright, several contexts in parallel) were tried to cut that
        down but measured *slower* on this machine - the CPU has no spare
        headroom for parallel Chromium rendering, so contention outweighs
        any overlap benefit. Kept sequential; this method runs on its own,
        less-frequent schedule (~15-20 min) rather than the 5-min
        Pinnacle/Loro/quick-Swisslos cadence, so ~290s fits comfortably.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                page = browser.new_page()
                seen_ids: set[str] = set()
                results: list[ScrapedRow] = []
                for slug in SOCCER_COUNTRY_SLUGS:
                    url = f"https://www.swisslos.ch/de/sporttip/sportwetten/fussball/{slug}"
                    try:
                        rows = self._fetch_page(page, url, sport)
                    except PlaywrightTimeoutError:
                        logging.getLogger("vb.sources.swisslos").warning(
                            "timed out loading %s - skipping this cycle", slug
                        )
                        continue
                    for row in rows:
                        if row.event.event_id not in seen_ids:
                            seen_ids.add(row.event.event_id)
                            results.append(row)
                return results
            finally:
                browser.close()

    def _fetch_page(self, page: Page, url: str, sport: str) -> list[ScrapedRow]:
        captured_at = datetime.now(timezone.utc)
        now_local = datetime.now(_ZURICH)

        page.goto(url, wait_until="load", timeout=self.timeout_ms)
        try:
            page.wait_for_selector('[id^="sportsSportsGrid_row_"]', timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            # A real, if uncommon, case: some country pages currently have
            # no fixtures listed at all (nothing scheduled) rather than a
            # load failure - treat as zero rows, not an error.
            return []
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

    @staticmethod
    def _parse_row(row_el, competition: str, sport: str, captured_at: datetime, now_local: datetime) -> Optional["ScrapedRow"]:
        """Sync-Playwright entry point: pulls id/texts off a real (or fake,
        in tests) ElementHandle, then hands off to the pure _parse_row_data
        so the async fetch path (_fetch_page_async) can share the same
        parsing logic without needing an ElementHandle at all.
        """
        row_id = row_el.get_attribute("id") or ""
        texts = row_el.eval_on_selector_all(
            "*",
            "els => els.filter(e => e.children.length === 0 && "
            "e.textContent.trim()).map(e => e.textContent.trim())",
        )
        return SwisslosClient._parse_row_data(row_id, texts, competition, sport, captured_at, now_local)

    @staticmethod
    def _parse_row_data(
        row_id: str, texts: list[str], competition: str, sport: str, captured_at: datetime, now_local: datetime
    ) -> Optional["ScrapedRow"]:
        m = _ROW_ID_RE.match(row_id)
        if not m:
            return None  # a sub-element (icon, tap-area, ...), not a top-level row

        # [home, away, date, count, ml_home, ml_draw, ml_away] is the
        # minimum (7) - some competitions (lower-tier leagues, cups like
        # DFB-Pokal) only ever show match-winner odds, no totals column.
        # When totals ARE shown, 3 more items follow (over, line, under),
        # for 10 total. Fewer than 7 means the row hasn't finished
        # rendering / no market at all - skip rather than guess.
        if len(texts) < 7:
            return None

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
                    max_bet_size=MAX_STAKE_CHF,
                )
            )
        except ValueError:
            pass  # not parseable as odds (e.g. market suspended/placeholder) - skip that market, keep the event

        if len(texts) >= 10:
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
                        max_bet_size=MAX_STAKE_CHF,
                    )
                )
            except ValueError:
                pass  # totals column present but not parseable - skip that market, keep the rest

        if not snapshots:
            return None
        return ScrapedRow(event=event, snapshots=snapshots)
