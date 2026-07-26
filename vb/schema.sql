-- Opportunity lifecycle storage: one `opportunity` header row per
-- continuous above-threshold period, linked to many `opportunity_snapshot`
-- rows (one per sample taken while it was open). See
-- `VB - methodology - addendum.md` for the model this implements.

CREATE TABLE IF NOT EXISTS opportunity (
    instance_id         TEXT PRIMARY KEY,
    -- Stable across re-occurrences of the same match+market+leg; groups
    -- instances together for re-occurrence analysis. NOT unique per row.
    market_key          TEXT NOT NULL,
    sport                TEXT NOT NULL,
    benchmark_site       TEXT NOT NULL,
    comparison_site      TEXT NOT NULL,
    market_type          TEXT NOT NULL,
    line                 REAL,                 -- NULL for match_winner
    selection            TEXT NOT NULL,        -- the specific leg being tracked
    first_cross_at       TEXT NOT NULL,        -- ISO8601 UTC
    resolved_at          TEXT,                 -- NULL while still open
    resolution_reason    TEXT,
    CHECK (resolution_reason IN (
        'dropped_below_threshold', 'market_suspended', 'event_started'
    ) OR resolution_reason IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_opportunity_market_key ON opportunity(market_key);

CREATE TABLE IF NOT EXISTS opportunity_snapshot (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_instance_id  TEXT NOT NULL REFERENCES opportunity(instance_id),
    captured_at              TEXT NOT NULL,   -- ISO8601 UTC
    edge_a                   REAL NOT NULL,   -- Method A: raw benchmark-vs-comparison edge
    edge_b                   REAL NOT NULL,   -- Method B: de-vigged EV edge
    benchmark_odds           REAL NOT NULL,
    comparison_odds          REAL NOT NULL,
    movement_source          TEXT NOT NULL,   -- benchmark | comparison | both | neither, vs. previous snapshot
    max_bet_size             REAL,
    -- All outcomes x all 4 books + each book's margin/overround, per the
    -- methodology's "full market snapshot" requirement. Kept as opaque
    -- JSON until the scrapers fix the exact per-book payload shape.
    full_market_json         TEXT NOT NULL,
    CHECK (movement_source IN ('benchmark', 'comparison', 'both', 'neither'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_opportunity
    ON opportunity_snapshot(opportunity_instance_id, captured_at);

-- Raw capture layer: every market a scraper reads, regardless of whether it
-- crosses any value threshold. This is the input the matching engine and
-- edge calculation run against; opportunity/opportunity_snapshot above are
-- downstream of it, not a replacement for it.

CREATE TABLE IF NOT EXISTS raw_event (
    site              TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    sport             TEXT NOT NULL,
    competition       TEXT NOT NULL,
    kickoff_utc       TEXT NOT NULL,   -- ISO8601 UTC
    raw_home_team     TEXT NOT NULL,
    raw_away_team     TEXT NOT NULL,
    PRIMARY KEY (site, event_id)
);

CREATE TABLE IF NOT EXISTS raw_market_snapshot (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    site              TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    market_type       TEXT NOT NULL,
    line              REAL,            -- NULL for match_winner
    outcomes_json     TEXT NOT NULL,   -- [{"selection": "home", "odds": 1.9}, ...]
    max_bet_size      REAL,            -- site's stated cap on this market, when exposed
    captured_at       TEXT NOT NULL,   -- ISO8601 UTC
    FOREIGN KEY (site, event_id) REFERENCES raw_event(site, event_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_market_snapshot_event
    ON raw_market_snapshot(site, event_id, captured_at);

-- Human review queue for REVIEW-tier event matches (vb.matching.MatchTier).
-- These are match candidates the fuzzy matcher isn't confident enough to
-- auto-trust (score >= REVIEW_THRESHOLD but < AUTO_THRESHOLD, or a
-- suspected home/away swap) - per the methodology, "a mismatched market
-- produces fake value", so they sit here until a human approves or
-- rejects them rather than being silently dropped or silently trusted.

CREATE TABLE IF NOT EXISTS event_match_review (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark_site        TEXT NOT NULL,
    benchmark_event_id    TEXT NOT NULL,
    comparison_site       TEXT NOT NULL,
    comparison_event_id   TEXT NOT NULL,
    score                 REAL NOT NULL,
    reasons_json          TEXT NOT NULL,
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    reviewed_at           TEXT,
    UNIQUE (benchmark_site, benchmark_event_id, comparison_site, comparison_event_id),
    CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_event_match_review_status
    ON event_match_review(status);

-- Settled result of one match+market+leg (vb.settlement). Keyed on the
-- BENCHMARK event, market, line and selection only - deliberately NOT
-- tied to a comparison site or to any specific opportunity instance,
-- because the actual outcome (who won, what the total was) doesn't
-- depend on which book's odds were being compared. The same settlement
-- row applies to every opportunity across every comparison site for that
-- leg. Evaluation joins opportunity rows to this table by matching
-- benchmark_site/benchmark_event_id/market_type/line/selection (which
-- market_key already embeds, minus the ":vs:{comparison_site}" suffix).

CREATE TABLE IF NOT EXISTS settlement (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark_site      TEXT NOT NULL,
    benchmark_event_id  TEXT NOT NULL,
    market_type         TEXT NOT NULL,
    line                REAL,
    selection           TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    home_goals          INTEGER,
    away_goals          INTEGER,
    settled_at          TEXT NOT NULL,
    source              TEXT,
    UNIQUE (benchmark_site, benchmark_event_id, market_type, line, selection),
    CHECK (outcome IN ('won', 'lost', 'push', 'half_won', 'half_lost'))
);

-- ============================================================
-- SCHEMA V2 — append-only, provenance-first persistence
-- ============================================================
-- Added per Phase 1 of the 2026-07-25 external audit's remediation
-- roadmap (audit 7-26/value-betting-system-audit-2026-07-25.md - see
-- F-02, F-03, F-06, F-09, F-11, F-15, F-17, and the entity table in
-- the audit's own §15.2). Every table above this line is the v1
-- schema, kept exactly as-is and read-only for the frozen legacy
-- dataset (experiment_id: legacy-development-2026-07, see
-- PROJECT_DOCUMENTATION.md §0) - nothing new is ever written there.
--
-- The core discipline every table below must uphold (this is the
-- actual fix for F-02, F-03, F-09, F-11): every row is inserted
-- exactly once at the moment it is truly created and never deleted or
-- rewritten. Where something needs to change over time (an episode
-- closing, a bet's accepted price becoming known), that is a narrow,
-- explicit UPDATE of specific late-bound columns on the SAME row
-- (e.g. signal_episode.ended_at), never a delete-and-reinsert of the
-- row or its children. A correction to something that already has a
-- terminal value (e.g. a wrong settlement) creates a NEW versioned
-- row that supersedes the old one; the old one is never touched.
-- Identity for every entity is a UUID minted in Python at the moment
-- of true creation (vb.identity.new_id()) - never a process-local
-- sequence counter, which is what actually caused F-02: a fresh
-- process has no memory of a prior process's counter state.

CREATE TABLE IF NOT EXISTS schema_migration (
    version      INTEGER PRIMARY KEY,
    applied_at   TEXT NOT NULL,
    description  TEXT NOT NULL
);

-- One real (scheduled or manual) invocation of the capture+pipeline
-- cycle. Replaces treating "a Python process ran" as an untracked,
-- unversioned event (F-06, F-16).
CREATE TABLE IF NOT EXISTS capture_run (
    id              TEXT PRIMARY KEY,
    scheduled_for   TEXT,             -- NULL for a manual/ad-hoc run
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    git_sha         TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    CHECK (status IN ('running', 'success', 'partial', 'failed'))
);

-- The result of one specific scraper within a capture_run. A signal
-- must never be computed from a source_run that isn't 'success' -
-- F-01's freshness gate and F-16's "no green run on partial failure"
-- both depend on this existing and being checked.
CREATE TABLE IF NOT EXISTS source_run (
    id                TEXT PRIMARY KEY,
    capture_run_id    TEXT NOT NULL REFERENCES capture_run(id),
    site              TEXT NOT NULL,
    mode              TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    event_count       INTEGER,
    snapshot_count    INTEGER,
    http_error_count  INTEGER NOT NULL DEFAULT 0,
    error_code        TEXT,
    error_summary     TEXT,
    CHECK (status IN ('running', 'success', 'partial', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_source_run_capture ON source_run(capture_run_id);
CREATE INDEX IF NOT EXISTS idx_source_run_site_status ON source_run(site, status, finished_at);

-- A source's own view of one event, versioned - a kickoff-time or
-- name correction creates a NEW row rather than overwriting the old
-- one (F-03's kickoff-correction requirement).
CREATE TABLE IF NOT EXISTS event_version (
    id             TEXT PRIMARY KEY,
    site           TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    source_run_id  TEXT NOT NULL REFERENCES source_run(id),
    sport          TEXT NOT NULL,
    competition    TEXT NOT NULL,
    kickoff_utc    TEXT NOT NULL,
    home_team      TEXT NOT NULL,
    away_team      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_version_site_event ON event_version(site, event_id, valid_from);

-- A stable real-world fixture identity that multiple sources' event_version
-- rows link to via event_match below (F-14's canonical identity).
CREATE TABLE IF NOT EXISTS canonical_event (
    id           TEXT PRIMARY KEY,
    sport        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- One source's link into a canonical_event, with orientation recorded
-- explicitly (F-14) so a directional market (handicap, 1X2) can never
-- be interpreted without knowing whether home/away came out swapped.
CREATE TABLE IF NOT EXISTS event_match (
    id                     TEXT PRIMARY KEY,
    canonical_event_id     TEXT NOT NULL REFERENCES canonical_event(id),
    event_version_id       TEXT NOT NULL REFERENCES event_version(id),
    role                   TEXT NOT NULL,
    orientation            TEXT NOT NULL,
    score                  REAL NOT NULL,
    score_components_json  TEXT NOT NULL,
    model_version          TEXT NOT NULL,
    tier                   TEXT NOT NULL,
    review_status          TEXT,
    decided_at             TEXT NOT NULL,
    CHECK (role IN ('benchmark', 'comparison')),
    CHECK (orientation IN ('same', 'swapped')),
    CHECK (tier IN ('auto', 'review', 'rejected')),
    CHECK (review_status IN ('approved', 'rejected') OR review_status IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_event_match_canonical ON event_match(canonical_event_id);
CREATE INDEX IF NOT EXISTS idx_event_match_version ON event_match(event_version_id);

-- Immutable market price, tied to the source_run/event_version that
-- produced it and both a source-reported observed_at (if any) and a
-- local received_at set at the moment of receipt - the pair F-01's
-- freshness/skew gate is computed from. This table is never pruned by
-- age the way v1's raw_market_snapshot is (F-10's finding about
-- pruning by id rather than time doesn't apply here because nothing
-- here is ever deleted at all).
CREATE TABLE IF NOT EXISTS market_snapshot_v2 (
    id                    TEXT PRIMARY KEY,
    source_run_id         TEXT NOT NULL REFERENCES source_run(id),
    event_version_id      TEXT NOT NULL REFERENCES event_version(id),
    market_type           TEXT NOT NULL,
    line                  REAL,
    outcomes_json         TEXT NOT NULL,
    max_bet_size          REAL,
    observed_at           TEXT,
    received_at           TEXT NOT NULL,
    request_started_at    TEXT,
    request_finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_v2_event
    ON market_snapshot_v2(event_version_id, market_type, line, received_at);

-- One immutable, hashed strategy configuration. A change to any
-- parameter is a NEW row with a new id, never an edit - so a
-- signal_episode's strategy_version always points at exactly the
-- rules that were active when it was created (F-06).
CREATE TABLE IF NOT EXISTS strategy_definition (
    id                TEXT PRIMARY KEY,
    signal_model      TEXT NOT NULL,
    threshold         REAL NOT NULL,
    max_age_s         REAL NOT NULL,
    max_skew_s        REAL NOT NULL,
    min_lead_time_s   REAL NOT NULL,
    config_json       TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (config_hash)
);

-- One continuous online above-threshold period - the direct
-- replacement for v1's `opportunity`. `id` is a UUID minted once, at
-- true creation, in Python - NEVER derived from a process-local
-- counter (that was F-02's entire root cause). `market_identity_id`
-- captures canonical_event + market_type + line + selection +
-- comparison site; the UNIQUE constraint on
-- (strategy_version, market_identity_id, started_at) is what makes a
-- genuine re-crossing safely distinguishable from an accidental
-- double-open, without ever needing a sequence number.
CREATE TABLE IF NOT EXISTS signal_episode (
    id                   TEXT PRIMARY KEY,
    strategy_version     TEXT NOT NULL REFERENCES strategy_definition(id),
    market_identity_id   TEXT NOT NULL,
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    end_reason           TEXT,
    CHECK (end_reason IN ('dropped_below_threshold', 'market_suspended', 'event_started') OR end_reason IS NULL),
    UNIQUE (strategy_version, market_identity_id, started_at)
);
CREATE INDEX IF NOT EXISTS idx_signal_episode_open ON signal_episode(market_identity_id, ended_at);

-- One (benchmark_snapshot, comparison_snapshot) pair evaluated under
-- one edge model - append-only replacement for v1's
-- opportunity_snapshot (which was DELETEd and rewritten wholesale on
-- every save_opportunity() call; that rewrite is what let F-02's
-- history-overwrite destroy an earlier episode's trajectory).
-- `eligible`/`reject_reason` record F-01's freshness/skew gate
-- outcome even when a pair is REJECTED, so a reject is itself an
-- auditable observation, not silence.
CREATE TABLE IF NOT EXISTS signal_observation (
    id                       TEXT PRIMARY KEY,
    episode_id               TEXT REFERENCES signal_episode(id),  -- NULL for a rejected/never-eligible observation
    decision_time            TEXT NOT NULL,
    benchmark_snapshot_id    TEXT NOT NULL REFERENCES market_snapshot_v2(id),
    comparison_snapshot_id   TEXT NOT NULL REFERENCES market_snapshot_v2(id),
    edge_model               TEXT NOT NULL,
    edge                     REAL NOT NULL,
    eligible                 INTEGER NOT NULL,
    reject_reason            TEXT,
    CHECK (eligible IN (0, 1)),
    CHECK (reject_reason IN ('benchmark_stale', 'comparison_stale', 'snapshot_skew', 'source_failed') OR reject_reason IS NULL),
    UNIQUE (episode_id, benchmark_snapshot_id, comparison_snapshot_id, edge_model)
);
CREATE INDEX IF NOT EXISTS idx_signal_observation_episode ON signal_observation(episode_id, decision_time);

-- Exactly one decision per idempotency_key (F-09's "at most one bet
-- per event+market+line+selection+site+strategy_version" policy is
-- enforced by what the caller hashes into that key, not by this
-- table itself).
CREATE TABLE IF NOT EXISTS bet_decision (
    id                     TEXT PRIMARY KEY,
    strategy_version       TEXT NOT NULL REFERENCES strategy_definition(id),
    signal_observation_id  TEXT NOT NULL REFERENCES signal_observation(id),
    decided_at             TEXT NOT NULL,
    decision               TEXT NOT NULL,
    reason                 TEXT NOT NULL,
    intended_odds          REAL,
    intended_stake         REAL,
    idempotency_key        TEXT NOT NULL UNIQUE,
    CHECK (decision IN ('bet', 'skip'))
);

-- The paper (or, later, real) execution outcome of one bet_decision.
-- Populated by Phase 5's execution model (vb/execution.py) - schema
-- only for now.
CREATE TABLE IF NOT EXISTS bet_execution (
    id               TEXT PRIMARY KEY,
    decision_id      TEXT NOT NULL REFERENCES bet_decision(id),
    requested_at     TEXT NOT NULL,
    responded_at     TEXT,
    status           TEXT NOT NULL,
    requested_odds   REAL NOT NULL,
    accepted_odds    REAL,
    requested_stake  REAL NOT NULL,
    accepted_stake   REAL,
    external_bet_id  TEXT,
    CHECK (status IN ('accepted', 'rejected', 'partial', 'price_changed'))
);
CREATE INDEX IF NOT EXISTS idx_bet_execution_decision ON bet_execution(decision_id);

-- Predefined closing-line reference for CLV (F-14.4 in the audit's
-- proposed roadmap). Populated by Phase 5's vb/closing.py - schema
-- only for now.
CREATE TABLE IF NOT EXISTS closing_snapshot (
    id                  TEXT PRIMARY KEY,
    canonical_event_id  TEXT NOT NULL REFERENCES canonical_event(id),
    market_type         TEXT NOT NULL,
    line                REAL,
    selection           TEXT NOT NULL,
    captured_at          TEXT NOT NULL,
    consensus_odds       REAL NOT NULL,
    source_json          TEXT NOT NULL
);

-- Origin of a final score - the auditable replacement for v1
-- settlement.source being a bare string (F-17). Populated by Phase
-- 6's settlement rework - schema only for now.
CREATE TABLE IF NOT EXISTS result_evidence (
    id                   TEXT PRIMARY KEY,
    canonical_event_id   TEXT NOT NULL REFERENCES canonical_event(id),
    provider             TEXT NOT NULL,
    provider_event_id    TEXT,
    source_url           TEXT,
    retrieved_at         TEXT NOT NULL,
    home_goals           INTEGER,
    away_goals           INTEGER,
    status               TEXT NOT NULL,
    raw_payload_hash     TEXT,
    reviewer             TEXT,
    reviewed_at          TEXT
);

-- Versioned settlement result - a correction creates a new row with
-- supersedes_id pointing at the old one; the old row is never edited
-- or deleted (F-17). Populated by Phase 6 - schema only for now.
CREATE TABLE IF NOT EXISTS settlement_version (
    id                 TEXT PRIMARY KEY,
    settlement_key     TEXT NOT NULL,
    evidence_id        TEXT NOT NULL REFERENCES result_evidence(id),
    algorithm_version  TEXT NOT NULL,
    result             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    supersedes_id      TEXT REFERENCES settlement_version(id),
    CHECK (result IN ('won', 'lost', 'push', 'half_won', 'half_lost'))
);
CREATE INDEX IF NOT EXISTS idx_settlement_version_key ON settlement_version(settlement_key, created_at);

-- One evaluation report's full provenance (F-06) - code SHA, config
-- hash, exact DB snapshot hash, and cutoff, so a report can never be
-- mistaken for describing a different code/data state than it
-- actually used. Populated by Phase 6's evaluation rework - schema
-- only for now.
CREATE TABLE IF NOT EXISTS evaluation_run (
    id                TEXT PRIMARY KEY,
    code_sha          TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    db_snapshot_hash  TEXT NOT NULL,
    data_cutoff       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    metrics_json      TEXT NOT NULL
);
