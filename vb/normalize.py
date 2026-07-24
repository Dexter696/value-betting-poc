"""Text and odds-line normalization for cross-site market matching.

Two problems live here:

1. Team/competition names differ across sites — accents, language
   (Swisslos/Loro can surface French), club-suffix conventions
   ("FC Basel" vs "Basel" vs "Bale"). normalize_team_name() collapses
   these to a comparable form before fuzzy matching.

2. Handicap lines are quoted from different perspectives by different
   books (home -1.5 vs away +1.5 is the same line). canonical_handicap_line()
   puts every line into a single home-perspective number so "-1.5" from
   one site and "+1.5 (away)" from another are recognized as the same
   market instead of silently compared as different ones.
"""

from __future__ import annotations

import re
import unicodedata

from unidecode import unidecode

# National-team names that Swiss French-language sites use and that won't
# fuzzy-match their English equivalents on string similarity alone
# (e.g. "allemagne" vs "germany" share almost no characters). Extend as
# real feed data surfaces more cases. Keys/values are already lowercase.
FRENCH_TO_ENGLISH_COUNTRIES: dict[str, str] = {
    "allemagne": "germany",
    "angleterre": "england",
    "autriche": "austria",
    "belgique": "belgium",
    "croatie": "croatia",
    "danemark": "denmark",
    "ecosse": "scotland",
    "espagne": "spain",
    "france": "france",
    "grece": "greece",
    "hongrie": "hungary",
    "italie": "italy",
    "pays-bas": "netherlands",
    "pays bas": "netherlands",
    "pologne": "poland",
    "portugal": "portugal",
    "republique tcheque": "czech republic",
    "serbie": "serbia",
    "suede": "sweden",
    "suisse": "switzerland",
    "turquie": "turkey",
    "pays de galles": "wales",
    "irlande": "ireland",
    "irlande du nord": "northern ireland",
}

# Swiss club names in French/German that don't fuzzy-match their common
# English/international spelling. Swisslos and Loro are Swiss-market
# sites, so this list matters more than a generic European club dictionary.
CLUB_ALIASES: dict[str, str] = {
    "bale": "basel",
    "fc bale": "basel",
    "young boys": "young boys",
    "servette": "servette",
    "lausanne-sport": "lausanne",
    "lausanne sport": "lausanne",
    "grasshopper": "grasshoppers",
    "grasshoppers zurich": "grasshoppers",
    "zuerich": "zurich",
    "fc zurich": "zurich",
    "saint-gall": "st gallen",
    "saint gall": "st gallen",
    "san gallo": "st gallen",
    "lucerne": "luzern",
    "lugano": "lugano",
    "sion": "sion",
}

# Generic club-name tokens that carry no matching signal and should be
# stripped before comparison. Deliberately conservative: only tokens that
# are pure organizational boilerplate, never anything that could be part
# of a team's distinguishing name (e.g. NOT "real", "united", "city").
_STOPWORD_TOKENS = {
    "fc", "cf", "ac", "sc", "sk", "fk", "cd", "ss", "us", "asd", "ssd",
    "ubc", "afc", "cfc", "club", "calcio", "futbol", "football",
    "bsc", "tsv", "vfl", "vfb", "sv", "fsv",
}

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")


def _strip_accents_lower(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = unidecode(text)
    return text.lower().strip()


def _basic_clean(text: str) -> str:
    text = _strip_accents_lower(text)
    text = _NON_ALNUM_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def normalize_team_name(raw_name: str) -> str:
    """Collapse a raw team/country name to a comparable canonical form.

    Order matters: alias/translation lookups run on the lightly-cleaned
    (accent-stripped, lowercased) string *before* stopword stripping,
    because some aliases (e.g. "fc bale") include the boilerplate token
    that would otherwise be removed first.
    """
    cleaned = _basic_clean(raw_name)

    if cleaned in FRENCH_TO_ENGLISH_COUNTRIES:
        cleaned = FRENCH_TO_ENGLISH_COUNTRIES[cleaned]
    if cleaned in CLUB_ALIASES:
        cleaned = CLUB_ALIASES[cleaned]

    tokens = [t for t in cleaned.split(" ") if t and t not in _STOPWORD_TOKENS]
    result = " ".join(tokens) if tokens else cleaned
    return result


def _translate_embedded_countries(text: str) -> str:
    """Replace French country/nationality names embedded anywhere in the
    string with their English equivalent (e.g. "Super League Suisse" ->
    "Super League switzerland"). Longest keys first so multi-word names
    like "pays de galles" aren't shadowed by a shorter overlapping key.
    """
    for french, english in sorted(FRENCH_TO_ENGLISH_COUNTRIES.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{re.escape(french)}\b", english, text)
    return text


def normalize_competition(raw_name: str) -> str:
    """Lighter-touch normalization for competition/league names.

    Competition naming varies a lot (abbreviations, sponsor names) and is
    used only as a secondary matching signal, so this stays conservative:
    accents/case/punctuation only, no stopword stripping. Embedded country
    names do get translated (e.g. "...Suisse" / "...Switzerland"), since
    French-language competition names routinely carry one.
    """
    cleaned = _basic_clean(raw_name)
    cleaned = _translate_embedded_countries(cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def canonical_handicap_line(side: str, raw_line: float) -> float:
    """Express an Asian handicap line from the home team's perspective.

    A book might quote "home -1.5" or, equivalently, "away +1.5" for the
    exact same market. Without canonicalizing, those two outcomes would
    look like different lines and never get matched. `side` must be
    "home" or "away".
    """
    side = side.lower()
    if side == "home":
        return raw_line
    if side == "away":
        return -raw_line
    raise ValueError(f"side must be 'home' or 'away', got {side!r}")
