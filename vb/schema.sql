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
