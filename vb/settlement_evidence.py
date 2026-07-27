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

Deliberately does NOT wire raw-response archiving into any scraper's
HTTP layer here — archive_raw_response() is the hashing primitive a
scraper would call, but deciding WHERE the archived bytes actually get
written (local disk vs. object storage) is an infrastructure decision
that belongs with Phase 2's VPS migration, and touching every
scraper's live HTTP path is a materially different, higher-blast-radius
change than everything else in this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .identity import content_hash_bytes, new_id
from .models import MarketType, ResultEvidence, Selection, SettlementVersion
from .settlement import settle
from .storage import current_settlement_version, save_result_evidence, save_settlement_version

ALGORITHM_VERSION = "settle-v1"  # bump whenever vb.settlement.settle()'s logic changes meaning


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
    """
    key = settlement_key(canonical_event_id, market_type, line, selection)
    result = settle(market_type, line, selection, home_goals, away_goals)
    existing = current_settlement_version(conn, key)

    version_id = new_id()
    save_settlement_version(conn, SettlementVersion(
        id=version_id, settlement_key=key, evidence_id=evidence_id, algorithm_version=ALGORITHM_VERSION,
        result=result.value, created_at=created_at, supersedes_id=existing.id if existing else None,
    ))
    return version_id
