"""Automated match-result lookup (ESPN's public, unauthenticated
scoreboard API), so settling a tracked opportunity doesn't require a
human to look up and type in the final score by hand.

site.api.espn.com's JSON endpoints are undocumented but widely used by
hobby odds/stats projects - no key, no auth, no rate limit trouble
observed. Coverage isn't universal: confirmed live (2026-07-24) that
several countries our Swisslos full-breadth sweep covers have no ESPN
league at all (Poland, Croatia, Slovakia, Slovenia, South Korea, Hungary,
Bulgaria). COUNTRY_TO_ESPN_CODE only lists confirmed-working codes;
anything else is left for manual settlement (scripts/record_result.py)
rather than guessed at.

Matching a Pinnacle event to an ESPN fixture happens in two stages:
  1. Narrow to one ESPN league via the benchmark event's own competition
     string. Pinnacle formats this as "{Country} - {League}" (confirmed
     live against /sports/29/leagues, e.g. "France - Ligue 1"), which
     splits cleanly into a country lookup (COUNTRY_TO_ESPN_CODE) plus a
     league-name lookup (CUP_SLUGS for cups, else a top/second-flight
     name heuristic).
  2. Within that league's fixtures for the date, require a *strict*
     team-name similarity match (both home and away, via the same
     normalize_team_name() + rapidfuzz combo vb.matching already uses)
     before accepting a score. Getting stage 1 wrong (e.g. two countries
     that both call their top flight "Bundesliga") just means stage 2
     finds no fixture and this returns None (safe no-op, falls back to
     manual entry) - it does NOT risk settling against the wrong match,
     since team names across countries essentially never collide at the
     similarity threshold used here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import requests
from rapidfuzz import fuzz

from ..normalize import normalize_team_name

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer"
MIN_TEAM_SIMILARITY = 88.0  # rapidfuzz 0-100 scale; strict on purpose, see module docstring

# A cup/qualifier match ESPN marks complete can carry a status name
# other than the plain "STATUS_FULL_TIME" this used to require
# exactly - STATUS_FINAL_AET (extra time) and STATUS_FINAL_PEN
# (penalty shootout) were both confirmed live 2026-07-29 to have
# completed=True and a real final score, but were silently excluded
# by the old exact-match check, sitting unsettled forever. Confirmed
# ESPN's own `score` field for a STATUS_FINAL_PEN match already
# reflects the pre-shootout (90+extra-time) goal count - the correct
# value for standard 1X2/Asian-Handicap/Totals settlement, where a
# penalty shootout only decides who advances, not the match result -
# so no separate shootout-score handling is needed for either status.
_COMPLETED_STATUS_NAMES = {"STATUS_FULL_TIME", "STATUS_FINAL_AET", "STATUS_FINAL_PEN"}

# Pinnacle's country name (lowercased) -> ESPN's soccer country code.
# Confirmed live against ESPN's league directory - NOT exhaustive, only
# what's actually been seen/tested. Extend as new countries show up in
# real capture data.
COUNTRY_TO_ESPN_CODE: dict[str, str] = {
    "england": "eng", "germany": "ger", "spain": "esp", "italy": "ita",
    "france": "fra", "netherlands": "ned", "portugal": "por", "scotland": "sco",
    "usa": "usa", "united states": "usa", "mexico": "mex", "brazil": "bra",
    "argentina": "arg", "uruguay": "uru", "chile": "chi", "china": "chn",
    "turkey": "tur", "austria": "aut", "belgium": "bel", "sweden": "swe",
    "norway": "nor", "denmark": "den", "romania": "rou", "czech republic": "cze",
    "greece": "gre", "bolivia": "bol", "colombia": "col", "ecuador": "ecu",
    "peru": "per", "paraguay": "par", "venezuela": "ven", "costa rica": "crc",
    "honduras": "hon", "guatemala": "gua", "el salvador": "slv",
    "japan": "jpn", "saudi arabia": "ksa", "india": "ind", "south africa": "rsa",
    "russia": "rus", "australia": "aus", "switzerland": "sui",
}

# Countries Pinnacle offers markets for that ESPN has no soccer league
# for at all (confirmed live 2026-07-24) - listed explicitly so a gap
# reads as "checked, unavailable" rather than "not yet looked at".
KNOWN_UNCOVERED_COUNTRIES = {
    "poland", "croatia", "slovakia", "slovenia", "south korea", "hungary", "bulgaria",
}

# Specific cup/competition names that don't follow the generic
# tier-1/tier-2 naming pattern. Checked before the generic heuristic.
CUP_SLUGS: dict[tuple[str, str], str] = {
    ("germany", "dfb pokal"): "ger.dfb_pokal",
    ("germany", "dfb-pokal"): "ger.dfb_pokal",
    ("england", "fa cup"): "eng.fa",
    ("england", "efl cup"): "eng.league_cup",
    ("england", "league cup"): "eng.league_cup",
    ("england", "carabao cup"): "eng.league_cup",
    ("spain", "copa del rey"): "esp.copa_del_rey",
    ("italy", "coppa italia"): "ita.coppa_italia",
    ("france", "coupe de france"): "fra.coupe_de_france",
}

# Competition names with no country prefix on Pinnacle (group is a
# confederation, e.g. "Europe"/"CONCACAF", not a country) - matched
# against the competition string directly.
INTERNATIONAL_SLUGS: dict[str, str] = {
    "champions league qualifiers": "uefa.champions_qual",
    "champions league": "uefa.champions",
    "europa league qualifiers": "uefa.europa_qual",
    "europa league": "uefa.europa",
    "conference league qualifiers": "uefa.europa.conf_qual",
    "conference league": "uefa.europa.conf",
    "copa sudamericana": "conmebol.sudamericana",
    "copa libertadores": "conmebol.libertadores",
    "world cup": "fifa.world",
    "european championship": "uefa.euro",
    "nations league": "uefa.nations",
    "friendlies women": "fifa.friendly.w",
    "friendlies": "fifa.friendly",
}

# Local league names that map to ESPN's generic ".1"/".2" tier suffixes.
# Checked only after CUP_SLUGS, so a cup competition never gets
# misread as a tier-1 league.
_TOP_FLIGHT_NAMES = {
    "premier league", "division 1", "liga mx", "brasileirao", "serie a",
    "primera division", "liga profesional", "liga pro", "eredivisie", "ligue 1",
    "bundesliga", "super lig", "allsvenskan", "eliteserien", "superliga",
    "liga i", "liga 1", "premiership", "liga auf uruguaya", "super league",
    "major league soccer", "first liga",
    # "liga 1" added alongside the existing "liga i" (roman numeral) -
    # confirmed live 2026-07-28 that Pinnacle formats Romania's top
    # flight with an arabic "1", not the roman numeral "I" this set
    # already expected, so it was silently falling through to manual
    # entry despite the country/slug being otherwise fully supported.
    # "major league soccer"/"first liga" (USA/Czech Republic) were
    # confirmed live the same day: 10 unsettled MLS matches and 3
    # unsettled Czech First Liga matches were sitting unrecognized even
    # though usa.1/cze.1 both return real, complete fixtures on ESPN -
    # the country+slug mapping was already correct, only the league-name
    # string was missing from this set.
}
_SECOND_TIER_NAMES = {
    "championship", "division 2", "serie b", "ligue 2", "2. bundesliga",
    "segunda division", "primera b", "challenge league", "superettan",
    # "superettan" (Sweden's 2nd tier) confirmed live 2026-07-28 -
    # swe.2 is a real, working ESPN slug, just never listed here.
}


def _espn_slug_for(competition: str) -> Optional[str]:
    text = competition.strip().lower()

    # "Club Friendlies" (pre-season club fixtures) is NOT the same thing as
    # the "friendlies"/"friendlies women" entries below, which are full
    # INTERNATIONAL (nation vs nation) friendlies under fifa.friendly -
    # confirmed live that slug carries zero club fixtures. ESPN doesn't
    # expose pre-season club friendlies under one enumerable slug at all
    # (each is its own small one-off tournament), so this is a real,
    # permanent gap - excluded explicitly here so it reads as "checked,
    # can't place it" rather than silently matching the wrong slug and
    # failing every single cycle forever.
    if text.startswith("club friendlies"):
        return None

    for name, slug in INTERNATIONAL_SLUGS.items():
        if name in text:
            return slug

    if " - " not in competition:
        return None  # no country prefix and no international-name match - can't place it

    country, league = (p.strip().lower() for p in competition.split(" - ", 1))
    if country in KNOWN_UNCOVERED_COUNTRIES:
        return None
    code = COUNTRY_TO_ESPN_CODE.get(country)
    if code is None:
        return None

    for (cup_country, cup_league), slug in CUP_SLUGS.items():
        if cup_country == country and cup_league in league:
            return slug

    if league in _TOP_FLIGHT_NAMES:
        return f"{code}.1"
    if league in _SECOND_TIER_NAMES:
        return f"{code}.2"
    return None  # not a name pattern we recognize - leave for manual entry


# Pinnacle's team names lean on English nicknames/abbreviations that
# share too few characters with ESPN's full display name for fuzzy
# string similarity to catch on its own (e.g. "Man City" vs "Manchester
# City" scores ~70/100 even with token-sort). An explicit expansion
# table beats loosening the similarity threshold, which was tried and
# rejected: token_set_ratio (permissive about missing tokens) scored
# derby-rival pairs like "AC Milan" vs "Inter Milan" and unrelated clubs
# sharing a name like "Barcelona" vs "Barcelona SC" (Ecuador) at 100/100
# - a real false-positive risk for a system that settles bets from this.
# Keys are already normalize_team_name()'d.
_TEAM_ALIASES: dict[str, str] = {
    "man city": "manchester city", "man utd": "manchester united", "man united": "manchester united",
    "spurs": "tottenham hotspur", "psg": "paris saint germain", "wolves": "wolverhampton wanderers",
    "leicester": "leicester city", "newcastle": "newcastle united", "west ham": "west ham united",
    "brighton": "brighton and hove albion", "nottm forest": "nottingham forest",
    "dortmund": "borussia dortmund", "bayern": "bayern munich",
    # Confirmed live 2026-07-28 against real, currently-unsettled
    # matches: Pinnacle's short/English club name vs ESPN's fuller/
    # native-language displayName scored well below MIN_TEAM_SIMILARITY
    # (55-78/100) despite being the exact same real fixture on the exact
    # same date in the exact same league - not a stray one-off, the same
    # short-vs-long-name pattern the existing aliases above already
    # exist for, just for different clubs.
    "kalmar": "kalmar ff", "mjallby": "mjallby aif", "lyngby": "lyngby boldklub",
    "zhejiang": "zhejiang professional",
    "copenhagen": "f c kobenhavn",  # Pinnacle: "FC Copenhagen" (English); ESPN: "F.C. København" (Danish)
}


def _team_similarity(a: str, b: str) -> float:
    na, nb = normalize_team_name(a), normalize_team_name(b)
    na, nb = _TEAM_ALIASES.get(na, na), _TEAM_ALIASES.get(nb, nb)
    return fuzz.token_sort_ratio(na, nb)


def find_result(competition: str, home_team: str, away_team: str, kickoff_utc: datetime) -> Optional[tuple[int, int]]:
    """Look up a finished match's final score. Returns None (never
    raises) if the competition isn't mapped, ESPN has no matching
    fixture, or no fixture on that date clears MIN_TEAM_SIMILARITY on
    both sides - all of those are "couldn't automate this one", not
    errors, and the caller should fall back to manual entry.
    """
    slug = _espn_slug_for(competition)
    if slug is None:
        return None

    date_from = (kickoff_utc - timedelta(days=1)).strftime("%Y%m%d")
    date_to = (kickoff_utc + timedelta(days=1)).strftime("%Y%m%d")
    try:
        resp = requests.get(f"{BASE_URL}/{slug}/scoreboard", params={"dates": f"{date_from}-{date_to}"}, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except (requests.RequestException, ValueError):
        return None

    best_score = 0.0
    best_result: Optional[tuple[int, int]] = None
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = comp.get("status") or {}
        if not status.get("type", {}).get("completed") or status.get("type", {}).get("name") not in _COMPLETED_STATUS_NAMES:
            continue  # only settle from a fully finished match - not live/postponed/abandoned

        competitors = comp.get("competitors") or []
        espn_home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        espn_away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if espn_home is None or espn_away is None:
            continue
        eh_name = (espn_home.get("team") or {}).get("displayName", "")
        ea_name = (espn_away.get("team") or {}).get("displayName", "")

        pair_score = min(_team_similarity(home_team, eh_name), _team_similarity(away_team, ea_name))
        if pair_score > best_score:
            try:
                candidate = (int(espn_home["score"]), int(espn_away["score"]))
            except (KeyError, ValueError, TypeError):
                continue
            best_score, best_result = pair_score, candidate

    return best_result if best_score >= MIN_TEAM_SIMILARITY else None
