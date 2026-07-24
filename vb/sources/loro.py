"""Loro (Loterie Romande) JouezSport scraper (headless Playwright).

Reads the same rendered odds list a visitor sees at
jeux.loro.ch/sports/hub/240?sport=FOOT — not an internal API call, for the
same reason as vb.sources.swisslos: Loro's real feed is behind
authenticated/obfuscated infrastructure not designed for scripted
consumption, so this renders the actual page instead.

Two things confirmed by hand (2026-07-23) that shape this module:

1. Row elements are plain MUI/emotion divs with content-hashed class names
   (e.g. "css-1h33tyh") that are NOT stable — the same page reloaded in a
   fresh browser context produced a *different* hash ("css-khquoo"). A
   class-name selector is useless here. Instead, rows are found
   structurally: each match row has exactly one leaf text node containing
   just "X" (the draw-odds column label), so rows are located by finding
   "X" leaves and climbing to the ancestor whose descendant leaf-text count
   falls in the range a real row produces (10-14). This only depends on
   the row's *shape*, not any generated class name.

2. Odds are loaded lazily per-row via an IntersectionObserver (or similar)
   — only rows within the viewport get their price data fetched. A normal
   1280x720 viewport only ever populates ~5 rows with odds, no matter how
   long you wait or how much you `wait_for_timeout`. The fix used here is
   an oversized viewport (1280x8000) so every row counts as "visible" on
   load and all of them fetch odds in one pass. Scrolling and re-reading
   was tried first and is unreliable (rows can unmount their price widget
   again once scrolled away) — the tall-viewport approach avoids that
   entirely.

Date/competition context isn't repeated per row on this aggregate listing
(unlike the "best bets" widget, which does inline both) — matches are
grouped under standalone section headers ("AUJOURD'HUI", "DEMAIN",
"SAM. 25.07", ...). This module walks all leaf text nodes in document
order once and attributes the most recently seen header to each row as it
goes, rather than trying to select header/row pairs independently.

Only match-winner (1X2) is available in this list view — no totals/
handicap column here (unlike Swisslos's default grid). TODO: check
individual match pages for more markets, same as Swisslos's handicap gap.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from ..models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection

SITE = "loro.ch"
FOOTBALL_URL = "https://jeux.loro.ch/sports/hub/240?sport=FOOT"

_ZURICH = ZoneInfo("Europe/Zurich")

_WEEKDAY_DATE_RE = re.compile(r"^[a-zé]{2,4}\.\s*(\d{1,2})\.(\d{1,2})$", re.IGNORECASE)


def _parse_kickoff(day_word: str, time_text: str, now_local: datetime) -> Optional[datetime]:
    """Parse Loro's French relative date strings: "aujourd'hui" / "demain"
    / "sam. 25.07" (weekday abbrev + DD.MM), plus a separate "HH:MM" text.
    `now_local` anchors relative words and must be in Europe/Zurich time.
    """
    tm = re.match(r"^(\d{1,2}):(\d{2})$", time_text.strip())
    if not tm:
        return None
    hour, minute = int(tm.group(1)), int(tm.group(2))

    day_word = day_word.strip().lower()
    if day_word == "aujourd'hui":
        local_date = now_local.date()
    elif day_word == "demain":
        local_date = (now_local + timedelta(days=1)).date()
    else:
        dm = _WEEKDAY_DATE_RE.match(day_word)
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


@dataclass
class ScrapedRow:
    event: RawEvent
    snapshots: list[MarketSnapshot]


# Finds match rows structurally (via the "X" draw-odds leaf, see module
# docstring) and attributes each to the most recently seen date-section
# header, walking the whole document once in order. Returns a flat list of
# {header, texts} with texts = [competition, "Home - Away", "HH:MM", home,
# home_odds, "X", draw_odds, away, away_odds, boost?] in document order.
_EXTRACT_ROWS_JS = r"""
() => {
  const xNodes = [...document.querySelectorAll('*')].filter(
    e => e.children.length === 0 && e.textContent.trim() === 'X'
  );
  const rowRootsArr = [];
  const seen = new Set();
  for (const xNode of xNodes) {
    let el = xNode;
    let found = null;
    for (let i = 0; i < 10 && el; i++) {
      const leaves = [...el.querySelectorAll('*')].filter(e => e.children.length === 0 && e.textContent.trim());
      if (leaves.length >= 10 && leaves.length <= 14) { found = el; break; }
      el = el.parentElement;
    }
    if (found && !seen.has(found)) { seen.add(found); rowRootsArr.push(found); }
  }

  const allLeaves = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 && e.textContent.trim());
  const dateHeaderRe = /^(AUJOURD'HUI|DEMAIN|[A-ZÉ]{2,4}\.\s*\d{1,2}\.\d{1,2})$/i;

  const rowsOut = [];
  let currentHeader = null;
  let currentRowRoot = null;
  let currentRowTexts = [];

  function flushRow() {
    if (currentRowRoot) rowsOut.push({ header: currentHeader, texts: currentRowTexts });
    currentRowRoot = null;
    currentRowTexts = [];
  }

  for (const leaf of allLeaves) {
    const text = leaf.textContent.trim();
    const owningRow = rowRootsArr.find(r => r.contains(leaf));
    if (owningRow) {
      if (owningRow !== currentRowRoot) {
        flushRow();
        currentRowRoot = owningRow;
      }
      currentRowTexts.push(text);
    } else {
      flushRow();
      if (dateHeaderRe.test(text)) currentHeader = text;
    }
  }
  flushRow();
  return rowsOut;
}
"""


class LoroClient:
    def __init__(self, headless: bool = True, timeout_ms: int = 30000):
        self.headless = headless
        self.timeout_ms = timeout_ms

    def fetch_football(self, sport: str = "soccer") -> list[ScrapedRow]:
        captured_at = datetime.now(timezone.utc)
        now_local = datetime.now(_ZURICH)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                # Oversized viewport so every row is "visible" and fetches
                # its lazily-loaded odds on first paint - see module
                # docstring point 2.
                page = browser.new_page(viewport={"width": 1280, "height": 8000})
                page.goto(FOOTBALL_URL, wait_until="load", timeout=self.timeout_ms)
                # No reliable selector to wait on here: date headers use a
                # typographic apostrophe inconsistently and row elements
                # have no stable hook (see module docstring) - a fixed
                # settle time after "load" is what was proven to work.
                page.wait_for_timeout(6000)

                raw_rows = page.evaluate(_EXTRACT_ROWS_JS)

                if not raw_rows:
                    # Diagnostic for "0 rows" failures that don't raise an
                    # exception (geo-block page, consent wall, slow render
                    # on an unfamiliar host) - logged rather than silently
                    # returning empty, since this scraper runs unattended.
                    logging.getLogger("vb.sources.loro").warning(
                        "zero rows extracted - title=%r url=%r body_sample=%r",
                        page.title(), page.url, page.inner_text("body")[:800],
                    )

                results: list[ScrapedRow] = []
                seen_event_ids: set[str] = set()
                for raw_row in raw_rows:
                    row = self._parse_row(raw_row["header"], raw_row["texts"], sport, captured_at, now_local)
                    if row is not None and row.event.event_id not in seen_event_ids:
                        seen_event_ids.add(row.event.event_id)
                        results.append(row)
                return results
            finally:
                browser.close()

    @staticmethod
    def _parse_row(
        header: Optional[str], texts: list[str], sport: str, captured_at: datetime, now_local: datetime
    ) -> Optional["ScrapedRow"]:
        # competition, "Home - Away", time, home, home_odds, "X", draw_odds, away, away_odds, [boost]
        if header is None or len(texts) < 9:
            return None

        competition, time_text = texts[0], texts[2]
        home, home_odds_txt, draw_odds_txt, away, away_odds_txt = texts[3], texts[4], texts[6], texts[7], texts[8]

        kickoff = _parse_kickoff(header, time_text, now_local)
        if kickoff is None:
            return None

        try:
            odds_home, odds_draw, odds_away = float(home_odds_txt), float(draw_odds_txt), float(away_odds_txt)
        except ValueError:
            return None

        # Loro's list view has no stable per-event id exposed in the DOM at
        # inspection time; derive one from the match+kickoff instead. This
        # is good enough to dedupe within one scrape, but will NOT survive
        # across scrape cycles as a stable market_key the way Pinnacle's or
        # Swisslos's real event ids do - see project notes on market_key.
        event_id = f"{home}|{away}|{kickoff.isoformat()}"

        event = RawEvent(
            site=SITE,
            sport=sport,
            competition=competition,
            kickoff_utc=kickoff,
            raw_home_team=home,
            raw_away_team=away,
            event_id=event_id,
        )
        snapshot = MarketSnapshot(
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
        return ScrapedRow(event=event, snapshots=[snapshot])
