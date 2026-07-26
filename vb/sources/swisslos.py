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

Only match-winner (1X2) and totals show in the default grid view. A real,
Pinnacle-comparable 2-way Asian Handicap market does exist (confirmed
live 2026-07-24), but only on each match's own detail page under an
"Asiatisch" tab — NOT the aggregate grid, and NOT the grid's own generic
"Handicap" tab (that one is 3-way/European-style, virtual-score handicap,
a different market shape our pipeline doesn't model). Getting it means
one extra page load per match (fetch_asian_handicap /
fetch_all_handicaps below) - expensive at ~7s/page across hundreds of
matches, so this runs on its own, much slower schedule than the grid
sweep, not every cycle.

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


_TRAILING_COLUMN_LABELS_RE = re.compile(r"(?<=[^\W\d_])\d.*$", re.DOTALL)


def _competition_name(header_text: str) -> str:
    # Header text glues the market-column labels onto the end, e.g.
    # "Qualifikation, Europa League1  X  2  Over  Tore  Under" - the
    # glued labels always start with a digit fused directly onto the
    # last letter of the name (no space). Splitting at the FIRST digit
    # anywhere (the original approach) silently destroyed any
    # competition whose real name starts with a digit, e.g. Swiss
    # league tiers like "2. Bundesliga" or "3. Liga" (audit F-14) - the
    # leading "2" has nothing before it, so the lookbehind here never
    # matches there, and only the genuinely glued suffix gets stripped.
    return _TRAILING_COLUMN_LABELS_RE.sub("", header_text).strip()


_ASIAN_HANDICAP_HEADING = "Asiatisches Handicap"
_ASIAN_HANDICAP_ENTRY_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*Team\s*([12])\s*\n\s*([\d.]+)")


def _parse_asian_handicap_section(body_text: str) -> list[tuple[float, float, float]]:
    """Parse the full-match "Asiatisches Handicap" block out of the match
    detail page's body text into (home_perspective_line, home_odds,
    away_odds) tuples. "Team 1" is the home team, "Team 2" away -
    confirmed by position ("Team 1" always corresponds to the same team
    listed first/as "1" in the same page's Endergebnis/1X2 section).

    Only the full-match block, not the "1. Halbzeit"/"2. Halbzeit"
    (half-time) variants that follow it on the same page and use an
    identical entry format - isolated by slicing between the first
    occurrence of the heading and the next (which is always
    "1. Halbzeit - Asiatisches Handicap", itself containing the heading
    as a substring).
    """
    first_idx = body_text.find(_ASIAN_HANDICAP_HEADING)
    if first_idx == -1:
        return []
    second_idx = body_text.find(_ASIAN_HANDICAP_HEADING, first_idx + 1)
    section = body_text[first_idx:second_idx if second_idx != -1 else None]

    entries: dict[tuple[str, float], float] = {}
    for line_str, team, odds_str in _ASIAN_HANDICAP_ENTRY_RE.findall(section):
        entries[(team, float(line_str))] = float(odds_str)

    markets: list[tuple[float, float, float]] = []
    seen_lines: set[float] = set()
    for (team, line), home_odds in entries.items():
        if team != "1" or line in seen_lines:
            continue
        away_odds = entries.get(("2", -line))
        if away_odds is None:
            continue  # one side missing/suspended - skip rather than guess
        seen_lines.add(line)
        markets.append((line, home_odds, away_odds))
    return markets


@dataclass
class ScrapedRow:
    event: RawEvent
    snapshots: list[MarketSnapshot]
    # The match's own detail-page URL (e.g.
    # ".../fussball/schweiz/super-league/team-a-vs-team-b?t=17849952") -
    # captured off the row's <a href> since it embeds a "t=" id that
    # doesn't match the grid row's own asw:event:{id} and can't be
    # reconstructed. Only the Asian Handicap market needs this (see
    # fetch_asian_handicap) - None if a row had no <a> (shouldn't happen
    # in practice, but not asserted on).
    detail_url: Optional[str] = None


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

    def fetch_all_handicaps(self, rows: list[ScrapedRow]) -> list[ScrapedRow]:
        """Visits every given row's own detail page (one navigation per
        match - see module docstring on why this is expensive and runs
        on its own slow schedule) and returns a new ScrapedRow per match
        that had a usable Asian Handicap section, containing only the
        ASIAN_HANDICAP snapshots (caller saves these alongside, not
        instead of, the match-winner/totals snapshots already captured
        by fetch_football()/fetch_all_countries()).

        Rows without a detail_url (shouldn't normally happen) are
        skipped, not guessed at.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                page = browser.new_page()
                results: list[ScrapedRow] = []
                for row in rows:
                    if row.detail_url is None:
                        continue
                    try:
                        snapshots = self._fetch_asian_handicap(page, row.detail_url, row.event)
                    except PlaywrightTimeoutError:
                        logging.getLogger("vb.sources.swisslos").warning(
                            "timed out loading handicap page for %s vs %s - skipping",
                            row.event.raw_home_team, row.event.raw_away_team,
                        )
                        continue
                    if snapshots:
                        results.append(ScrapedRow(event=row.event, snapshots=snapshots, detail_url=row.detail_url))
                return results
            finally:
                browser.close()

    def _fetch_asian_handicap(self, page: Page, detail_url: str, event: RawEvent) -> list[MarketSnapshot]:
        captured_at = datetime.now(timezone.utc)

        page.goto(detail_url, wait_until="load", timeout=self.timeout_ms)
        page.wait_for_timeout(1500)

        tab = page.query_selector("text=/Asiatisch/")
        if tab is None:
            return []  # this match's page doesn't offer the Asian Handicap tab at all
        tab.click(force=True)
        page.wait_for_timeout(1200)

        body_text = page.inner_text("body")
        snapshots = []
        for line, home_odds, away_odds in _parse_asian_handicap_section(body_text):
            snapshots.append(
                MarketSnapshot(
                    event=event,
                    market_type=MarketType.ASIAN_HANDICAP,
                    line=line,
                    outcomes=(
                        Outcome(Selection.HOME, home_odds),
                        Outcome(Selection.AWAY, away_odds),
                    ),
                    captured_at=captured_at,
                    max_bet_size=MAX_STAKE_CHF,
                )
            )
        return snapshots

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
        link_el = row_el.query_selector("a")
        detail_url = link_el.get_attribute("href") if link_el else None
        return SwisslosClient._parse_row_data(row_id, texts, competition, sport, captured_at, now_local, detail_url)

    @staticmethod
    def _parse_row_data(
        row_id: str, texts: list[str], competition: str, sport: str, captured_at: datetime, now_local: datetime,
        detail_url: Optional[str] = None,
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
        return ScrapedRow(event=event, snapshots=snapshots, detail_url=detail_url)
