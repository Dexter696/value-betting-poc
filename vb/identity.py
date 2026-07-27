"""Stable identity for schema-v2 entities.

Every v2 row's primary key is minted here, once, at the moment of true
creation - never derived from a process-local counter or from
resuming/reconstructing state. That distinction is the actual fix for
the audit's F-02 finding: v1's `OpportunityTracker._instance_seq`
started at 0 in every fresh process, so a re-crossing after a close (in
a new process, which has no memory of the prior process's counter)
could mint the same instance_id a genuinely earlier, unrelated
opportunity had already used - and the storage layer's upsert then
silently overwrote that earlier row's data. A UUID minted at creation
can never collide with a UUID from a different real creation event,
regardless of how many separate processes touch this market_key over
however many restarts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


def new_id() -> str:
    """A fresh, globally-unique identity for one real creation event.
    Call this exactly once per entity, at the moment it's truly created -
    never to "look up" or "resume" an identity for something that might
    already exist (that's what content-based keys like
    signal_episode's (strategy_version, market_identity_id, started_at)
    UNIQUE constraint are for).
    """
    return str(uuid.uuid4())


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(obj: dict) -> str:
    """Deterministic JSON serialization - same logical config always
    produces the same exact string, so config_hash() is stable across
    process/platform/dict-ordering differences.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def content_hash(obj: dict) -> str:
    """SHA-256 of an object's canonical JSON form - used for
    strategy_definition.config_hash (so an identical config always
    resolves to the same row rather than creating a duplicate) and for
    evaluation_run's code/config/data provenance hashes.
    """
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def content_hash_bytes(data: bytes) -> str:
    """SHA-256 of raw bytes, unencoded - for archiving a raw source
    response (vb.settlement_evidence's content-addressed archive, F-17)
    where the payload isn't JSON-serializable data but the literal HTTP
    response body."""
    return hashlib.sha256(data).hexdigest()
