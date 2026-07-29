"""Result evidence + versioned settlement — Phase 6 steps 1/2 (partial)
of the audit's remediation roadmap (2026-07-25), fixing F-17.

F-17's finding: settlement arithmetic itself is correct (quarter-line
handicaps split properly, every stored outcome recomputes cleanly),
but the ORIGIN of a result wasn't auditable — v1's settlement.source
is a bare string like "manual" or "manual:websearch", not real
evidence. This module is the fix: every settlement traces back to a
ResultEvidence row (an automatic source's real response, hashed and
archivable; a manual correction's URL/reviewer/reason) and every
settlement RESULT is a SettlementVersion row — insert-only, so a later
correction creates a new version pointing at the one it supersedes
rather than silently rewriting history the way v1's mutable
`settlement` table could.

Deliberately does NOT wire raw-response BYTE archiving into any odds
scraper's HTTP layer — archive_raw_response() is the hashing primitive
a scraper would call, but deciding WHERE the archived bytes actually
get written (local disk vs. object storage) is an infrastructure
decision that belongs with Phase 2's VPS migration, and touching all
three odds scrapers' live HTTP paths is a materially higher-blast-
radius change than everything else in this module.

The one exception (2026-07-29): `record_settlement_for_event`'s
optional `raw_payload_hash`/`source_url` come from
`vb.sources.results.find_result_with_evidence`, which hashes ESPN's
scoreboard response WITHOUT storing the bytes anywhere - the hash is
tamper-evidence for whatever score got recorded, not a byte-for-byte
archive. Lower blast radius than the odds scrapers (one already-
audited settlement path, not three live capture paths) and doesn't
need a storage-location decision since nothing new is being persisted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .identity import content_hash_bytes, new_id
from .models import (
    CanonicalEvent,
    EventMatchV2,
    MarketType,
    MatchOrientation,
    MatchRole,
    MatchTierV2,
    ResultEvidence,
    Selection,
    SettlementVersion,
)
from .settlement import settle
from .storage import current_settlement_version, save_canonical_event, save_event_match_v2, save_result_evidence, save_settlement_version

ALGORITHM_VERSION = "settle-v1"  # bump whenever vb.settlement.settle()'s logic changes meaning
BOOTSTRAP_MODEL_VERSION = "bootstrap-v1"


def get_or_create_canonical_event(conn, event_version_id: str, sport: str, now: datetime) -> str:
    """Bootstrap canonical event identity for a single source event.

    NOT the full cross-site fusion vb.market_mapping's matching engine
    will eventually provide once a real labeled negative dataset exists
    to calibrate it against (see that module's own docstring on why
    that's still deferred) - this just gives settlement evidence and
    closing-consensus collection a real, stable id to reference NOW,
    anchored on a single source event rather than a cross-site fusion.

    If `event_version_id` is already linked to a canonical_event
    (checked via event_match), reuses it. Otherwise creates a new
    CanonicalEvent plus a single event_match row - score=1.0,
    orientation=SAME, tier=AUTO, model_version="bootstrap-v1" - linking
    them. A bootstrap "match" is trivially correct (it's the event
    matched to itself), not a real cross-site pairing, so it's
    intentionally distinguishable from a genuine vb.market_mapping
    match by its model_version.
    """
    existing = conn.execute(
        "SELECT canonical_event_id FROM event_match WHERE event_version_id = ? LIMIT 1", (event_version_id,)
    ).fetchone()
    if existing is not None:
        return existing[0]

    canonical_id = new_id()
    save_canonical_event(conn, CanonicalEvent(id=canonical_id, sport=sport, created_at=now))
    save_event_match_v2(conn, EventMatchV2(
        id=new_id(), canonical_event_id=canonical_id, event_version_id=event_version_id,
        role=MatchRole.BENCHMARK, orientation=MatchOrientation.SAME, score=1.0, score_components={},
        model_version=BOOTSTRAP_MODEL_VERSION, tier=MatchTierV2.AUTO, decided_at=now,
    ))
    return canonical_id


def archive_raw_response(payload: bytes) -> str:
    """Content-addressed hash of a raw source response — the "archive
    and hash every source response" half of F-17's fix. A caller that
    has somewhere durable to store `payload` should name the archived
    file/object after this hash and pass the hash itself as
    ResultEvidence.raw_payload_hash.
    """
    return content_hash_bytes(payload)


def settlement_key(canonical_event_id: str, market_type: MarketType, line: Optional[float], selection: Selection) -> str:
    """Stable identity for "this specific leg's settlement," independent
    of any one opportunity/episode instance — mirrors v1's settlement
    table being keyed by (event, market_type, line, selection) rather
    than by a particular opportunity, since the same leg's real-world
    result never depends on which book flagged it."""
    return f"{canonical_event_id}:{market_type.value}:{line}:{selection.value}"


def record_result_evidence(
    conn,
    canonical_event_id: str,
    provider: str,
    retrieved_at: datetime,
    status: str,
    home_goals: Optional[int] = None,
    away_goals: Optional[int] = None,
    provider_event_id: Optional[str] = None,
    source_url: Optional[str] = None,
    raw_payload_hash: Optional[str] = None,
    reviewer: Optional[str] = None,
    reviewed_at: Optional[datetime] = None,
) -> str:
    """Record one attempt to determine a canonical_event's result — an
    automatic source's real response (provider, raw_payload_hash) or a
    manual correction (source_url, reviewer, reviewed_at). Always
    insert-only: a second, later capture attempt for the same event is
    a NEW row, keeping every attempt visible rather than only the
    latest.
    """
    evidence_id = new_id()
    save_result_evidence(conn, ResultEvidence(
        id=evidence_id, canonical_event_id=canonical_event_id, provider=provider, retrieved_at=retrieved_at,
        status=status, provider_event_id=provider_event_id, source_url=source_url, home_goals=home_goals,
        away_goals=away_goals, raw_payload_hash=raw_payload_hash, reviewer=reviewer, reviewed_at=reviewed_at,
    ))
    return evidence_id


def record_settlement_for_event(
    conn, benchmark_site: str, benchmark_event_id: str, provider: str, home_goals: int, away_goals: int, now: datetime,
    raw_payload_hash: Optional[str] = None, source_url: Optional[str] = None,
) -> int:
    """The real live-wiring entry point: given a benchmark event's final
    score, records ONE ResultEvidence and a SettlementVersion for every
    DISTINCT (market_type, line, selection) leg ever tracked for this
    event (via signal_episode.market_identity_id - see
    vb.pipeline.market_key), regardless of whether a bet was actually
    placed on it, matching the project's "capture everything" discipline.

    Skips silently (returns 0) if this benchmark event was never
    captured into schema v2 (no event_version row) - can't anchor a
    canonical_event on something that doesn't exist yet, which is
    expected for events that predate the v2 shadow pipeline going live.

    Multiple comparison sites tracking the SAME real leg (e.g. HOME on
    1X2 vs both swisslos.ch and loro.ch) produce different
    market_identity_id strings (comparison_site is part of the key,
    see market_key()) but the same real-world settlement outcome -
    deduped here so record_settlement_version is only called once per
    genuinely distinct leg, not once per comparison site.

    Returns the number of distinct legs settled (0 if the event was
    never captured into v2, or was captured but no leg was ever
    tracked for it) - not necessarily the number of NEW rows inserted,
    since record_settlement_version() is itself a no-op for a leg
    whose result hasn't actually changed since it was last settled.
    """
    from .pipeline import parse_market_identity  # local import: avoids a module-level cycle with vb.pipeline

    row = conn.execute(
        "SELECT id, sport FROM event_version WHERE site = ? AND event_id = ? ORDER BY valid_from DESC LIMIT 1",
        (benchmark_site, benchmark_event_id),
    ).fetchone()
    if row is None:
        return 0
    event_version_id, sport = row

    canonical_id = get_or_create_canonical_event(conn, event_version_id, sport=sport, now=now)
    evidence_id = record_result_evidence(
        conn, canonical_id, provider=provider, retrieved_at=now, status="final",
        home_goals=home_goals, away_goals=away_goals,
        raw_payload_hash=raw_payload_hash, source_url=source_url,
    )

    market_identity_rows = conn.execute(
        "SELECT DISTINCT market_identity_id FROM signal_episode WHERE market_identity_id LIKE ?",
        (f"{benchmark_site}:{benchmark_event_id}:%",),
    ).fetchall()

    legs_settled: set[tuple] = set()
    for (market_identity_id,) in market_identity_rows:
        parsed = parse_market_identity(market_identity_id)
        leg_key = (parsed.market_type, parsed.line, parsed.selection)
        if leg_key in legs_settled:
            continue
        legs_settled.add(leg_key)
        record_settlement_version(
            conn, canonical_id, parsed.market_type, parsed.line, parsed.selection, evidence_id,
            home_goals=home_goals, away_goals=away_goals, created_at=now,
        )
    return len(legs_settled)


def record_settlement_version(
    conn,
    canonical_event_id: str,
    market_type: MarketType,
    line: Optional[float],
    selection: Selection,
    evidence_id: str,
    home_goals: int,
    away_goals: int,
    created_at: datetime,
) -> str:
    """Compute the settlement result via vb.settlement.settle() (which
    now raises on an invalid selection/line — F-19) and record it as a
    new SettlementVersion. If a version already exists for this
    settlement_key, the new row's `supersedes_id` points at it — the
    old row is never touched, so an evaluation that already ran against
    it stays reproducible even after a correction.

    A no-op if the current version already has the exact same result:
    a genuine correction (a different result) always creates a new,
    versioned row, but re-settling identical data (e.g. a repeated
    backfill pass over already-settled events) would otherwise pile up
    redundant "corrections" that never actually changed anything -
    real, if harmless, noise in what's supposed to be an audit trail of
    genuine changes. Returns the EXISTING version's id in that case,
    not a new one.
    """
    key = settlement_key(canonical_event_id, market_type, line, selection)
    result = settle(market_type, line, selection, home_goals, away_goals)
    existing = current_settlement_version(conn, key)

    if existing is not None and existing.result == result.value:
        return existing.id

    version_id = new_id()
    save_settlement_version(conn, SettlementVersion(
        id=version_id, settlement_key=key, evidence_id=evidence_id, algorithm_version=ALGORITHM_VERSION,
        result=result.value, created_at=created_at, supersedes_id=existing.id if existing else None,
    ))
    return version_id
