# VB (Value Betting) — Project Documentation

**Purpose of this document:** a complete, verifiable account of what this system does, how it does it, and why every non-obvious design decision was made — written for audit by another LLM or a human reviewer who has no prior context. Every claim below is grounded in a specific file/line in the codebase as of commit `a9fe263` (2026-07-25), so it can be independently re-verified rather than taken on faith. Where something is a known limitation or an open question, it is stated as such rather than glossed over. This document has been kept current across two work sessions (2026-07-24 and 2026-07-25); §14 and §15 in particular carry a full, dated history of every real bug found and fixed, including one found by a formal self-audit of this document's own author's work (§14.6).

---

## 0. Legacy dataset notice (read this first)

**An independent external audit examined this project on 2026-07-25** (commit `e0a0e12ed0d6c7ff887066a47d0f630e2efff3e1`, database SHA-256 `7fe5770104cbfcf974174210bacc1cd8d8bfc4ceb87afbeb08c8c995e07ad504`) and returned a **NO-GO verdict** for real money and for any public claim of proven positive ROI. The audit found several real correctness bugs beyond what this document's own §14 bug history had caught — most seriously, a confirmed case where an opportunity re-crossing after a process restart could silently overwrite an earlier opportunity's recorded history (found live in 16 of 175 opportunities), a missing freshness/skew gate that lets the pipeline pair arbitrarily stale odds, and a Method B evaluation that checks the wrong entry point (only whether B's edge was already above threshold at Method A's entry snapshot, never scanning for B's own first crossing). The audit's own §6.1 also states plainly: **23 of the 59 settled Method-A legs entered before this project's first commit, and all 59 entered before the major duplicate-fix commits — none of the current ROI figures are attributable to the code as it exists today.**

Per the audit's own remediation roadmap, **Phase 0 ("freeze the old experiment") has been executed**:

1. The exact database the audit examined is archived read-only and immutably at GitHub release [`legacy-development-2026-07`](https://github.com/Dexter696/value-betting-poc/releases/tag/legacy-development-2026-07) — this asset will never be overwritten, unlike the `db-sync` release which the daily capture cycle keeps replacing.
2. The live dashboard now shows a permanent banner: *"Legacy development data — not attributable to current strategy"*, and the misleading Method-B/converge-filter headline framing has been relabeled to say plainly what those numbers are and are not.
3. Every dashboard build is now tagged with `experimentId: legacy-development-2026-07` (`scripts/build_dashboard.py`'s `EXPERIMENT_ID` constant), embedded in the data itself, not just prose.
4. **Going forward, no historical row is retroactively edited under the guise of a "fix."** Corrections are made as new rows/versions, not silent overwrites of old ones — this discipline starts now, ahead of the full append-only schema (Phase 1) the audit recommends.
5. This whole document, and everything in §1–§18 below, should be read as **describing a legacy development dataset and the codebase that produced it** — accurate as a record of what was built and why, but not as a claim that the resulting numbers demonstrate a working strategy. See §14.6-adjacent material and the audit document itself (`audit 7-26/value-betting-system-audit-2026-07-25.md`) for the complete findings and the proposed v2 architecture.

**Phase 1 ("schema v2 + append-only persistence") is now built and unit-tested**, as of commits `783343e`, `66bc38a`, `8d87b81`:

- `vb/schema.sql` gained 16 new append-only tables (§15.2 of the audit) alongside the untouched v1 schema — verified additive against a copy of the live 157MB production database (all 6 v1 tables byte-identical row-for-row before/after; `scripts/migrate_schema_v2.py` is the standalone, repeatable way to re-verify this).
- `vb/identity.py` mints UUID identity at true creation time, never a process-local counter — the direct fix for F-02 (a process-restart identity collision was previously destroying real opportunity history; confirmed live in 16/175 opportunities).
- `vb/episode.py`'s `EpisodeTracker` is the schema-v2 replacement for `vb.opportunity.OpportunityTracker`, built on that UUID identity.
- `vb/freshness.py`'s `check_freshness()` is F-01's fix — rejects stale or mutually-skewed benchmark/comparison quote pairs before they can open or extend an episode.
- `vb/storage.py`'s new v2 functions are insert-only except for a small number of explicit, narrow, guarded state-transition `UPDATE`s on the same row (never a delete-and-rewrite) — `prune_raw_snapshots()` was also fixed in place (F-10: it was ranking "latest" by `MAX(id)`, which a merge can violate; now ranks by `captured_at`).
- `vb/capture_v2.py` + `vb/pipeline.py`'s `run_cycle_v2()` bridge the new schema onto the *existing, unchanged* `vb.matching`/`vb.edge` engines, and fix F-04 (Method B was only ever checked at Method A's entry snapshot, never scanned for its own independent first crossing) by construction: each method's `StrategyDefinition` drives its own fully independent `EpisodeTracker`.

**Update, 2026-07-27 to 2026-07-29 — the v2 pipeline is now live, as a shadow alongside v1.** `scripts/scheduled_run.py` runs `run_cycle_v2` on every real scheduled cycle (`*/5 * * * *` on GitHub Actions) alongside the completely unchanged v1 `run_cycle()` — v1 remains the sole source of truth for the live dashboard and this document's own §1–§18 numbers; v2 is accumulating real, independent history. What's landed since Phase 1:

- **Phase 1 fix, F-03 (2026-07-29).** `vb/episode.py`'s `EpisodeTracker` closed an episode on `market_suspended`/`event_started` by stamping `ended_at` with the stale reading's own `received_at` (the last real ODDS observation, not when the close actually happened) and unconditionally recording a duplicate `signal_observation` even when no new snapshot had actually been fetched since kickoff - exactly the audit's reproduction (43/44 real `event_started` opportunities closing before their own kickoff in the legacy v1 dataset). Fixed per the audit's precise spec: `LegReadingV2` now carries `now` (process wall-clock time) and `kickoff_utc` separately from `received_at`; an `event_started` close stamps `ended_at = max(now, kickoff_utc)`, and a close with no new snapshot since the last observation reuses that observation's id instead of fabricating a new row (`vb.closing` still gets a valid `observation_id` to key off). v1's `opportunity.py`/`run_cycle()` has the same bug but was deliberately left as-is - it's the frozen legacy dataset's own producer (Phase 0), not something this remediation effort is patching incrementally; v2 is where correctness investment goes.
- **Post-audit cross-check, 2026-07-29.** Re-verified every F-01..F-21 finding against current code (not just the roadmap's own phase summaries) and fixed three more real, bounded gaps: F-16 (`scripts/scheduled_run.py` never exited non-zero on a total capture failure - a fully-broken cycle could look green in GitHub Actions; now exits 1 on `RunStatus.FAILED`, still 0 on `PARTIAL`), F-20 (`requirements.txt` was unpinned - now exact-pinned), F-11 (`scripts/merge_databases.py`'s `raw_event`/`event_match_review` merges always kept dest's row on conflict regardless of which side was actually newer/more-decided - `raw_event` now mirrors `save_raw_capture()`'s own live "latest wins" upsert semantics, and `event_match_review` now protects a real human review decision from ever being silently overwritten by a still-pending row from the other side). F-07 (GH Actions cadence/concurrency) and F-21 (pre-entry chart replay fidelity, v1-only) remain open - both need the VPS migration or are scoped to the legacy v1 path respectively, not more isolated code fixes.
- **Phase 3 (canonical matching, F-14).** `vb/market_mapping.py`'s `match_events_v2` — a real maximum-weight bipartite assignment (`scipy.optimize.linear_sum_assignment`) replacing greedy nearest-first matching, plus explicit `MatchOrientation` tracking so a detected home/away swap is actually remapped (selection + Asian Handicap line sign) rather than only logged as a warning string. Wired into `run_cycle_v2` via `find_leg_edges_v2`. Two real, live bugs were found and fixed while deploying this: a blocking filter that used exact string equality on a non-token-order-stable normalization (silently producing zero matches for weeks), and a `scipy` infeasibility crash once blocking started working. Also fixed the Swisslos competition-name parser (names starting with a digit, e.g. "2. Bundesliga", were being truncated to nothing) and a client-timezone bug that made every `swisslos.ch` kickoff render 2 hours early on GitHub Actions' UTC runners specifically (invisible in local dev, since this machine's system timezone happens to share Zurich's summer UTC offset). Calibrating the auto-accept threshold against a real labeled negative dataset is still blocked — the review queue's 100 reviewed candidates (as of this writing) are all genuinely correct matches, so there isn't yet a naturally-occurring negative example to calibrate against.
- **Phase 4 (fair probability, F-12/F-13).** `vb/fair_probability.py` adds power and odds-ratio de-vig methods (favorite-longshot-bias-aware, unlike plain proportional scaling) plus calibration metrics (log loss, Brier score, bucketed calibration, source dispersion). `vb/feature_dataset.py` builds a time-aligned, no-look-ahead feature dataset from real `signal_observation` history. **Betfair client built, 2026-07-29** (`vb/sources/betfair.py`) — a real, tested `BetfairClient` against the actual Betfair Exchange JSON-RPC API (interactive login, `listMarketCatalogue`/`listMarketBook`, MATCH_ODDS parsing, `effective_odds_after_commission()` for the exchange's win-only net-winnings commission). Not wired into `scheduled_run.py` yet, for two reasons that are genuinely blocked on the project owner, not on more code: (1) it needs `BETFAIR_APP_KEY`/`BETFAIR_USERNAME`/`BETFAIR_PASSWORD` — account creation and credential handling are things only the account owner can do; (2) whether Betfair should act as a *second benchmark* (cross-validating Pinnacle, the audit's literal ask) or as another *comparison* book like Swisslos/Loro is a real edge-detection semantics decision — market_key/episode-tracking currently assume one benchmark, so this needs a deliberate design call, not a silent bolt-on. Once credentials exist, wiring is a single `scheduled_run.py` capture step following the existing `PinnacleClient`/`SwisslosClient` pattern.
- **Phase 5 (online entry/execution, complete).** `vb/strategy.py`'s `ImmediateEntryPolicy`/`PersistentEntryPolicy` state machines, `vb/execution.py`'s idempotent decision recording and conservative-slippage-capped execution verification, `vb/exposure.py`'s per-event/per-site stake ceilings, and `vb/closing.py`'s closing-consensus/CLV collection are all wired into the live shadow pipeline (`vb/decision_runner.py`, `scripts/scheduled_run.py`). `vb/entry_policy_report.py` covers the audit's "store every transition" ask by reconstructing WAITING/DECIDED/ABANDONED state from existing observation history rather than adding a redundant schema table.
- **Phase 6 (settlement evidence + evaluation, core landed).** `vb/settlement_evidence.py` records versioned `ResultEvidence`/`SettlementVersion` rows for every real settlement, bootstrapped onto a per-source-event `canonical_event` (a deliberate placeholder for the full cross-site fusion Phase 3's matching will eventually provide once calibrated). `vb/evaluation_runner.py` + `vb/evaluation_v2.py` assemble real `ExecutedBet` rows and produce a real evaluation report — run for the first time against live data on 2026-07-29 (38 decisions, 28 unique events), and now actually wired into the live cron (F-06, 2026-07-29): `scripts/scheduled_run.py`'s `run_daily_evaluation()` generates a real `evaluation_run` row once daily, piggybacked on the existing `--full-handicaps` low-frequency trigger - before this it was built and tested but never invoked outside one-off manual runs. ESPN's raw scoreboard response is now hashed (not byte-archived) into evidence on every auto-settlement, 2026-07-29 (`find_result_with_evidence`, see below). Byte-level archiving of the three live odds-scraper HTTP paths stays deferred (needs Phase 2's storage-location decision); the dashboard JS rewrite from this era of scope was already completed in an earlier 2026-07-24 session, unrelated to this remediation roadmap.
- **Phase 2 (VPS)** has not started — blocked on infrastructure only the project owner can provision, not on more code.
- **Phase 7 (the actual pre-registered experiment).** The recording mechanism is now built, 2026-07-29: `vb/protocol.py`'s `freeze_protocol()`/`require_active_protocol()` on a new insert-only `experiment_protocol` table, mirroring `SettlementVersion`'s supersede-not-edit pattern (a later freeze under the same `name` marks the previous one `superseded_by`, never edits it). What's still blocked on the project owner, not on more code: the actual threshold/exposure/sample-size/stopping-rule *values* for a real confirmatory cohort — this module deliberately doesn't invent them. The v2 shadow pipeline continues to run ungated by this, since it's an accumulation phase, not the confirmatory run Phase 7 describes.

---

## 1. The hypothesis being tested

Soft (recreational-market) bookmakers — Swisslos.ch and Loro.ch, both Swiss state-licensed betting operators — occasionally quote odds that diverge from the "true" market price. Pinnacle.com, a sharp/low-margin book widely used in the odds-modeling community as a reference price, is treated as that true-price **benchmark**. When a soft book's price implies a better payout than Pinnacle's own price would justify, that's flagged as a **value bet**.

This project does not place real bets. It is a **paper-trading / hypothesis-testing system**: capture odds from all three books continuously, mechanically flag every crossing of a fixed edge threshold, track what happens to that edge over time, wait for the real match result, and evaluate — after the fact — whether flagged bets would actually have profited, using two competing methods of estimating "true" probability (see §6).

The core discipline enforced throughout the codebase (stated explicitly in nearly every module's docstring) is: **capture is unconditional, decisions are post-processing**. The pipeline never discards a reading because it looks uninteresting; entry-timing policy, which method to trust, and staking strategy are all decided later, against the full stored history, so those decisions can be revisited without re-collecting data.

---

## 2. High-level data flow

```
 ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
 │ Pinnacle.com │   │ Swisslos.ch │   │  Loro.ch    │      (scrapers, vb/sources/*)
 │ guest API    │   │ Playwright  │   │ Playwright  │
 └──────┬───────┘   └──────┬──────┘   └──────┬──────┘
        │                  │                  │
        └────────► save_raw_capture() ◄───────┘             (vb/storage.py)
                          │
              raw_event / raw_market_snapshot          (append-only time series)
                          │
                          ▼
              vb.pipeline.run_cycle()                  (once per comparison site)
              ├─ vb.matching  (event + market matching)
              ├─ vb.edge      (Method A + Method B, computed for EVERY matched leg)
              └─ vb.opportunity.OpportunityTracker      (threshold state machine)
                          │
                          ▼
              opportunity / opportunity_snapshot         (only legs that crossed 3%)
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
  vb.sources.results (ESPN)    scripts/record_result.py   (settlement, independent
      auto-settle                   (manual fallback)      of opportunity tracking)
              │                        │
              └──────────┬─────────────┘
                          ▼
                     settlement table
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
     vb/evaluation.py            scripts/build_dashboard.py
     (Method A vs B report,      (self-contained HTML, embedded JSON,
      bucketed, flat+Kelly ROI)   client-side filters/sim, deployed to
                                  GitHub Pages every cycle)
```

Everything left of "opportunity" runs every 5 minutes (plus two slower supplementary schedules — see §11). Settlement and the dashboard rebuild run every cycle too, so the whole system is effectively always-current.

---

## 3. Data sources and their quirks

### 3.1 Pinnacle.com — the benchmark (`vb/sources/pinnacle.py`)

- Reads Pinnacle's own internal "guest" API (`guest.api.arcadia.pinnacle.com`) — the same unauthenticated endpoint Pinnacle's website itself calls to render odds client-side. Not a personal credential; a publicly-known anonymous key used by other open odds-scraping projects.
- Two endpoints: `/leagues/{id}/matchups` (event list) and `/leagues/{id}/markets/straight` (prices). Markets are moneyline → `MATCH_WINNER`, spread → `ASIAN_HANDICAP`, total → `TOTALS`. Only `period == 0` (full match) is read; team totals and 1st/2nd-half markets are not modeled.
- Odds arrive in American format and are converted to decimal (`_decimal_odds`).
- `max_bet_size` comes directly from Pinnacle's own published `maxRiskStake` limit — no inference needed.
- **Known quirk (not a bug):** Pinnacle issues a **new, distinct `event_id`** for the same real-world fixture once it goes in-play, separate from the pre-match id. Confirmed by timing analysis during this session's duplicate-bet investigation. This causes `raw_event`/`raw_market_snapshot` rows to legitimately multiply per real match — but in every case checked, the in-play id's market never independently crossed the 3% threshold (Pinnacle typically pulls a match from the pre-match feed at kickoff, and the opportunity tracker closes on wall-clock kickoff time regardless — see §6.1), so this does not create duplicate *opportunities* or duplicate bets, only extra raw capture rows.

### 3.2 Swisslos.ch (`vb/sources/swisslos.py`) — comparison site #1

- No usable public API: the real data feed is behind an obfuscated, token-per-session endpoint. Instead of attempting to defeat that, the scraper drives a **headless Playwright browser** and reads the exact rendered DOM a real visitor sees — same content, no protection bypass involved.
- Two capture modes:
  - **Quick** (`fetch_football()`): a single page load of the "Alle Bewerbe" (all competitions) view. Runs every 5 minutes.
  - **Full country-breadth** (`fetch_all_countries()`): sequentially visits 42 separate per-country/competition URLs (`SOCCER_COUNTRY_SLUGS`, a static list hand-derived from Swisslos's own country picker on 2026-07-24 — will silently miss anything Swisslos adds later until re-derived). Runs on its own ~5-minute cadence, offset from the quick cycle (`7,27,47 * * * *` vs `*/5 * * * *`) so the two never collide.
  - A **concurrent** version of the full sweep was tried and measured *slower* than sequential on the machine used for development (no spare CPU headroom) — sequential was kept deliberately, not by default/oversight.
- Only match-winner and totals appear in the standard grid. A genuine 2-way Asian Handicap market (comparable to Pinnacle's spread market) exists but only on each match's own detail page under an "Asiatisch" tab — not the grid's own "Handicap" tab, which is a different (3-way, European-style) market not modeled here. Reaching it costs one extra ~7-second page load per match; across hundreds of matches this is tens of minutes, so it runs on its own once-daily schedule (`13 3 * * *`) rather than every cycle — most of the time, handicap odds shown are up to ~24h stale, an accepted tradeoff.
- `MAX_STAKE_CHF = 1000.0`: Swisslos doesn't expose a per-market limit like Pinnacle does. This is a flat, site-wide, per-bet cap confirmed live via the anonymous bet-slip validation message ("Dein maximaler Einsatz beträgt CHF 1'000.00"), reproduced identically across multiple matches/markets — recorded as a constant, not scraped per-row.
- Handles both German-language relative dates ("Heute"/"Morgen") and two different explicit date formats seen across different grid views.

### 3.3 Loro.ch (`vb/sources/loro.py`) — comparison site #2

- Same Playwright-rendering approach as Swisslos.
- Only match-winner is captured (`MAX_STAKE_CHF = 500.0`, same anonymous-validation-message technique as Swisslos). Asian Handicap was **deliberately not implemented** for Loro (explicit user decision during this project's development — "Loro skipped").

### 3.4 What "capture" actually persists

Every scrape cycle calls `save_raw_capture()` (`vb/storage.py:41`), which **upserts** the `raw_event` row (event metadata can be refined, e.g. kickoff-time corrections) and **appends** new rows to `raw_market_snapshot` — this table is a true time series, never overwritten. It is the single source of truth all downstream matching/edge/opportunity logic reads from; nothing about capture depends on whether any given reading turns out to be "interesting."

---

## 4. Cross-site matching engine (`vb/matching.py`)

Two independent problems, solved in two stages:

### 4.1 Event matching — "is this the same real-world fixture?"

`score_event_pair()` combines three signals into one weighted score:

```
total = 0.70 * team_name_score + 0.15 * competition_name_score + 0.15 * time_score
```

- **Hard time cutoff first**: a candidate more than 15 minutes (`TIME_TOLERANCE`) off the anchor's kickoff is rejected outright, before any name scoring runs — two fixtures 6 hours apart are never the same match no matter how similar the names look.
- **Team names**: `rapidfuzz.fuzz.token_sort_ratio` on both teams, after `normalize_team_name()` (see §4.3). Both the direct pairing (home↔home) and the swapped pairing (home↔away) are scored; if swapped scores notably higher (`SWAP_SUSPICION_MARGIN = 0.10`), that's a strong signal one site listed teams in the opposite order — this is flagged as suspicious and forced into human review regardless of the raw score, since a wrong home/away assignment would corrupt handicap-line and 1X2-selection mapping.
- Three-tier outcome: **AUTO** (score ≥ 0.75, trusted automatically), **REVIEW** (0.70–0.75, or any swap-suspicious match, queued for a human via `event_match_review`), **REJECT** (below 0.70, silently dropped, never surfaced).
- `AUTO_THRESHOLD` was originally 0.90, then **recalibrated to 0.75 on 2026-07-24** after a full manual audit: every one of 64 REVIEW-tier candidates in the 0.71–0.90 range was checked by hand and found to be a *correct* match — zero false positives. That's direct empirical evidence the scorer is reliable well below the original conservative cutoff (`vb/matching.py:41-48`), not a guess.
- `match_events()` is a **greedy** one-to-one matcher: each anchor (benchmark) event claims its single best-scoring still-unclaimed candidate; no candidate can be matched to two different anchors.

### 4.2 Market matching — "which specific line on site B corresponds to this line on site A?"

`match_markets()` pairs markets of the same `market_type` with the same canonical `line` (within `LINE_TOLERANCE = 0.01`, absorbing float rounding only — not a fuzzy match). A market with no line counterpart on the other site (e.g. it doesn't offer that particular handicap) is returned separately as "unmatched," not folded into the review queue — that's a coverage gap, not a matching failure.

### 4.3 Name normalization (`vb/normalize.py`)

- `normalize_team_name()`: strips accents/case/punctuation, translates French country names Swiss sites sometimes use ("Allemagne" → "Germany"), applies a hand-built Swiss-club alias table ("Bâle" → "Basel", "Zürich" → "Zurich", etc.), then strips organizational-boilerplate tokens ("FC", "SC", "Club", ...) — deliberately conservative, never strips a token that could be part of a team's distinguishing name.
- `canonical_handicap_line()`: re-expresses every handicap line from the **home team's perspective**, so "home −1.5" and "away +1.5" (the same market quoted two different ways) are recognized as identical rather than treated as two different lines.

---

## 5. Edge calculation — Method A vs Method B (`vb/edge.py`)

Both methods are computed for **every** matched leg, every cycle — which one to trust is a post-processing decision (per the project's core philosophy), not a capture-time filter. Only Method A's crossing of 3% ever triggers opportunity tracking (see §6.1); Method B's edge is stored alongside it for later comparison.

- **Method A (`raw_edge`)**: treats the benchmark's own published odds for that specific leg as if they were the fair price (ignoring the benchmark's own margin/vig). `edge = comparison_odds / benchmark_odds − 1`. Simple, but **systematically over-flags longshots** as value, because bookmakers typically load their margin disproportionately onto long odds (favorite-longshot bias) — so a longshot's raw implied probability is inflated more than a favorite's, making Method A structurally biased toward false positives on the longshot end.
- **Method B (`devigged_edge`)**: first removes the benchmark **market's** entire overround (not just the one leg — needs all outcomes) via proportional de-vig (`devig_proportional`: scale each outcome's implied probability down so they sum to exactly 1.0), then computes edge against that de-vigged fair probability. This corrects the *overall margin* but explicitly does **not** itself correct favorite-longshot bias — that correction is what the bucketed Method A vs Method B comparison in `vb/evaluation.py` exists to surface, not something baked into Method B's formula.

The dashboard's threshold slider (§12) currently applies to **Method A only** — an explicit, deliberate scope decision (see §12.2), not an oversight.

---

## 6. Opportunity lifecycle (`vb/opportunity.py`)

This is the piece of the model that caused the most back-and-forth during development, so it's documented here with maximum precision, including the exact distinction that was repeatedly clarified with the user.

### 6.1 Definition

An **opportunity** is a *continuous period*, not a single snapshot: it begins the instant a specific match+market+leg's Method-A edge first reaches ≥ 3% (`THRESHOLD = 0.03`, `vb/opportunity.py:27`), and it stays open — accumulating one snapshot row per capture cycle, however far the edge climbs — until exactly one of three things happens:

| `ResolutionReason` | Meaning |
|---|---|
| `DROPPED_BELOW_THRESHOLD` | The edge fell back under 3% on a later reading. |
| `MARKET_SUSPENDED` | The book pulled the market (temporarily or permanently). |
| `EVENT_STARTED` | Wall-clock time at pipeline-run time reached the benchmark event's kickoff. |

A later re-crossing of the *same* leg after it closes is a **new** `Opportunity` instance (`instance_id` gets a new `#N` suffix), linked to earlier instances only via a shared `market_key` — re-occurrence is analyzable later, but each continuous period is tracked and evaluated as its own bet.

### 6.2 The critical distinction: "resolved" ≠ "settled" ≠ "we would have bet it"

This came up repeatedly during development and is the single most important modeling nuance in the system:

- **`resolved_at` being set** means only that the *tracking* of that edge crossing stopped, for one of the three reasons above. It says **nothing** about whether the real-world match has finished, and nothing about whether a bet placed there would have been resolved yet.
- **`DROPPED_BELOW_THRESHOLD` and `MARKET_SUSPENDED` do not mean the match started or ended** — they can happen hours before kickoff, simply because the price gap closed (the very thing a value bettor would expect: soft books often correct toward the sharp price over time).
- **The betting hypothesis governing this project**: as soon as the edge crosses 3%, we assume a bet *could* have been placed at that price and stake — the entry is the crossing itself, independent of how long the window stayed open afterward. A `DROPPED_BELOW_THRESHOLD` resolution after 4 minutes is exactly as "bettable" as one that stayed open for 3 hours; **entry timing is the crossing, not the duration of the window** (this is the resolution of the user's explicit question "why do you consider those that dropped below threshold as we did not bet? time slot was too short?" — the answer is: they should *not* be excluded, and after this discussion the evaluation logic (§8) treats every crossing as a bet regardless of how it later resolved).
- **`is_open` staying `True` past kickoff is a real, acknowledged gap, not a bug**: once a match kicks off, Pinnacle removes it from its live-odds feed entirely — no more readings ever arrive for that leg. The tracker only closes an opportunity when it *receives* a new reading showing suspension/drop/kickoff; if Pinnacle stops sending anything at all, the tracker never gets that chance. `run_cycle()` (`vb/pipeline.py:198`) works around most of this by computing `event_started` from **wall-clock time**, independent of whether Pinnacle's feed is still returning fresh data — but see §15.2 for the residual edge case this doesn't fully close.
- **Settlement is a completely separate mechanism**, keyed only on `(benchmark_site, benchmark_event_id, market_type, line, selection)` — it doesn't care about opportunity tracking state at all, doesn't require `resolved_at` to be set, and reads the real final score from ESPN or manual entry once the actual match is over (see §7). A closed-but-unsettled opportunity and a still-open-but-actually-finished opportunity are both real, distinguishable states the dashboard surfaces separately.

### 6.3 Per-opportunity derived fields

Computed as `@property` methods on `Opportunity` (`vb/opportunity.py:100-128`), never stored redundantly:

- `entry_edge_a` / `entry_edge_b`: the **first** snapshot's edge (the crossing moment).
- `peak_edge_a` / `peak_snapshot`: the highest edge ever observed during the open period.
- `time_to_peak`: peak snapshot's timestamp minus `first_cross_at`.
- `convergence_time`: `resolved_at − first_cross_at` **if `resolved_at is not None`** — see §14.1 for a real bug this exact line once had.

### 6.4 Movement attribution

Each snapshot records whether the edge moved because the **benchmark** odds moved, the **comparison** odds moved, **both**, or **neither**, relative to the immediately preceding reading for that leg (`MovementSource`, compared with a `1e-6` float-noise epsilon so unchanged re-quotes don't flip-flop the label). This matters for interpretation: a gap that widens because the benchmark moved is possibly just newly-arrived information (injury news) that the soft book hasn't caught up to yet; a gap that widens because the comparison book alone drifted is closer to the "mispricing" signal the hypothesis is actually about.

### 6.5 `OpportunityTracker` mechanics

One tracker instance per watched leg (`market_key`), fed an ordered stream of `LegReading`s via `ingest()`. Two structural points worth flagging for audit purposes:

- **Duplicate-timestamp guard** (`vb/opportunity.py:209-227`): a reading whose `captured_at` is not strictly newer than the last one seen is treated as a no-op *unless* it signals `event_started` or `market_suspended` — those are computed from wall-clock time at pipeline-run time, not from `captured_at`, so a match can legitimately transition to "started" between two calls that otherwise see frozen, identical odds; that transition must still get through so the tracker can close.
- **Cross-run resumption** (`OpportunityTracker.resume()`, `vb/opportunity.py:172-207`): each scheduled pipeline run is a fresh Python process with no in-memory state. A still-open `Opportunity` is reloaded from SQLite (`load_open_opportunity`) and the tracker's internal state (sequence counter, last-seen reading) is reconstructed from it, so a real multi-cycle trajectory is built up incrementally across independent process runs rather than restarting from scratch every 5 minutes.

---

## 7. Settlement (`vb/settlement.py`, `vb/sources/results.py`)

### 7.1 Settlement logic

Given `(market_type, line, selection)` and a final score, `settle()` dispatches to the right function:

- `settle_match_winner`: straightforward — compares the actual winner (home/away/draw) to the selection.
- `settle_totals`: over/under vs. `home_goals + away_goals`; exact equality is a `PUSH`.
- `settle_handicap`: whole/half lines settle directly (`_settle_handicap_half_line` — adjusts the scoreline by the line, checks the sign). **Quarter lines** (e.g. −0.25, −0.75) are the interesting case: real sportsbooks split the stake across the two adjacent half/whole lines. This is modeled as the *combination* of both halves' outcomes rather than as two separate tracked bets — `_QUARTER_COMBINATIONS` maps `{WON}`→`WON`, `{WON, PUSH}`→`HALF_WON`, `{LOST, PUSH}`→`HALF_LOST`, `{LOST}`→`LOST`. `quarter_units = round(line * 4)`; a line is a quarter line iff that value is odd.

### 7.2 Automated settlement via ESPN (`vb/sources/results.py`)

- Uses ESPN's public, unauthenticated scoreboard API (`site.api.espn.com/apis/site/v2/sports/soccer`) — undocumented but widely used by hobby odds projects, no key/auth/observed rate limiting.
- Two-stage matching, deliberately conservative to avoid ever settling against the wrong fixture:
  1. **League placement**: Pinnacle's competition string ("Country - League") is parsed into a country + league name, mapped to ESPN's league slug via `COUNTRY_TO_ESPN_CODE` + either a specific cup lookup (`CUP_SLUGS`) or a generic tier-1/tier-2 name heuristic (`_TOP_FLIGHT_NAMES`/`_SECOND_TIER_NAMES`). International competitions (Champions League, Europa/Conference League + their qualifiers, Copa Sudamericana/Libertadores, World Cup, Euros, Nations League, friendlies) are matched directly by name, no country prefix. **Confirmed-zero-coverage countries are listed explicitly** (`KNOWN_UNCOVERED_COUNTRIES`: Poland, Croatia, Slovakia, Slovenia, South Korea, Hungary, Bulgaria) so a gap in the code reads as "checked, ESPN just doesn't have it" rather than "not yet looked at."
  2. **Team-name match within that league's fixture list for the date**: both home and away must independently clear `MIN_TEAM_SIMILARITY = 88.0` (rapidfuzz `token_sort_ratio` after `normalize_team_name()` + a small hand-built alias table for English nicknames ESPN's full names don't fuzzy-match well, e.g. "Man City" → "Manchester City").
  - **`token_sort_ratio` was deliberately chosen over `token_set_ratio`**: `token_set_ratio` is more permissive about missing/extra tokens, and testing found it scored "AC Milan" vs "Inter Milan" (different clubs) and "Barcelona" vs "Barcelona SC" (Ecuador — an unrelated club sharing a name) both at 100/100 — an unacceptable false-positive risk for a function that writes final settlement outcomes. `token_sort_ratio` plus the alias table was measurably safer.
- Wrong-league placement is safe by construction: if stage 1 mis-identifies the league, stage 2 simply finds no fixture clearing the similarity bar and returns `None` — it does not risk cross-country false-positive settlement (team names essentially never collide across countries at this threshold).
- Only settles from a match ESPN itself reports as `STATUS_FULL_TIME` and `completed`.

### 7.3 Manual fallback

`scripts/record_result.py` + a human looking up the score (used for leagues ESPN doesn't cover, and any auto-settle miss). `record_match_result()` (`vb/storage.py:514`) settles **every** distinct `(market_type, line, selection)` combination ever tracked as an opportunity for that benchmark event in one call — a human supplies the final score once per match, not once per leg.

### 7.4 What triggers the settlement worklist

`list_unsettled_matches()` (`vb/storage.py:408`) finds every benchmark event that (a) has at least one tracked opportunity, (b) is at least `kickoff_buffer_hours` (default 3h) past kickoff, and (c) has no settlement row yet. This is the shared worklist both `auto_settle()` (ESPN) and manual entry draw from.

---

## 8. Evaluation methodology (`vb/evaluation.py`)

Reads only already-settled data — computes nothing new, same "post-processing, not capture-time" philosophy as the rest of the project.

- **Bucketing**: legs are grouped by the benchmark's own entry odds into `favorite` (< 2.0), `mid` (2.0–4.0), `longshot` (≥ 4.0) — `DEFAULT_BUCKETS`. The stated purpose (module docstring): "if the raw method's longshot bucket underperforms while its favorite bucket holds up, that is the margin bias — not a failure of the hypothesis."
- **Same underlying bet set for both methods**: every settled leg was captured because Method A crossed 3% (the one fixed capture-time threshold) — Method A's stats cover all of them; Method B's stats cover the **subset** where Method B's own edge *also* would have flagged it (`method_b_would_flag`, `entry_edge_b >= THRESHOLD`). This avoids inferring divergence from two separately-drawn samples.
- **Two staking scenarios, both post-hoc views over the same settled bets** (per the methodology's own note that "betting 13:1 at the same stake as 2:1 inflates variance and biases the test"):
  - **Flat** (`flat_stake_profit`): every bet the same size.
  - **Fractional Kelly** (`kelly_stake_fraction` / `kelly_stake_profit`): `full_kelly = edge / (odds − 1)` — this reduces cleanly from the standard Kelly formula `f* = (b·p − q)/b` because `raw_edge`/`devigged_edge` already compute exactly the numerator `b·p − q` against these same comparison odds, so no separate probability input is needed. Scaled by `kelly_fraction` (default `DEFAULT_KELLY_FRACTION = 0.25`, quarter-Kelly). **Deliberately fractional, not full Kelly** — full Kelly is high-variance even with an accurate probability estimate, and Method A's estimate is known to be biased for longshots, so full-Kelly sizing would amplify exactly the bias this evaluation exists to detect. A negative edge clips the stake to 0 (never a short — only backing the comparison side that showed value is considered).
  - Each method's Kelly scenario sizes off **its own** edge estimate (Method A's bucket stats use `entry_edge_a`, Method B's use `entry_edge_b`) — each staking scenario reflects that method's own probability estimate, not the other's.
- **Hit rate** excludes pushes from the denominator (`is_win` returns `None` for a push), the standard sports-betting ROI convention; half-won/half-lost count as 0.75/0.25 win credit.

---

## 9. Database schema (`vb/schema.sql`)

Six tables, SQLite:

| Table | Role | Key design notes |
|---|---|---|
| `raw_event` | One row per (site, event_id) — static match metadata. | PK `(site, event_id)`. Upserted, never duplicated. |
| `raw_market_snapshot` | Every market reading, every cycle, regardless of edge. | The literal time series; unbounded growth (see §10.3 pruning). No uniqueness constraint — natural key for dedup purposes is `(site, event_id, market_type, line, captured_at)`, enforced only by application logic (`merge_databases.py`), not a DB constraint. |
| `opportunity` | One header row per continuous above-3%-threshold period. | PK `instance_id` (`"{market_key}#{seq}"`). `market_key` groups re-occurrences but is *not* unique per row. `CHECK` constraint on `resolution_reason`. |
| `opportunity_snapshot` | One row per capture cycle while an opportunity was open. | FK to `opportunity.instance_id`. Carries both edge methods, both books' odds, movement attribution, and the full multi-outcome market JSON for both sides of that particular comparison (not all 4 books at once — see `vb/pipeline.py:93-111`). |
| `event_match_review` | Human review queue for REVIEW-tier event matches. | `UNIQUE(benchmark_site, benchmark_event_id, comparison_site, comparison_event_id)`. `status` never touched by the pipeline once a human has set it. |
| `settlement` | Final result per `(benchmark_site, benchmark_event_id, market_type, line, selection)`. | **Deliberately not tied to any comparison site or opportunity instance** — the real-world outcome doesn't depend on which book's odds were being compared, so one settlement row serves every opportunity across every comparison site for that leg. **Critical SQLite gotcha handled explicitly**: the `UNIQUE` constraint includes a nullable `line` column (NULL for every match-winner leg), and standard SQL/SQLite never treats two NULLs as equal even under a UNIQUE index — so `INSERT ... ON CONFLICT` and naive `INSERT OR IGNORE` **both silently fail to dedupe** NULL-line rows. `save_settlement()` (`vb/storage.py:344`) works around this with an explicit `UPDATE` first, `INSERT` only if `rowcount == 0`, using `(line IS ? OR line = ?)` in the `WHERE` clause. The same fix had to be independently re-applied in `merge_databases.py` after it was found the hard way (see §14.2). |

---

## 10. Pipeline orchestration

### 10.1 `vb/pipeline.py::run_cycle()`

One call per (benchmark, comparison) site pair, per cycle:
1. Load each site's **latest** snapshot per `(event, market_type, line)` (`load_latest_market_snapshots` — always reflects whatever the last scrape wrote, however stale).
2. `classify_event_matches()` → AUTO-tier matches processed directly; REVIEW-tier ones are (a) written/refreshed into the review queue every cycle they still appear (`save_review_candidate`, upsert, never touches a human-set `status`) and (b) **also processed** if a human has already approved that exact pair (`load_approved_review_pairs`) — approving a review candidate changes what future cycles actually do, not just what's displayed.
3. `find_leg_edges()` computes both Method A and B for every matched leg (§5), unconditionally — filtering by threshold is the tracker's job, not this function's.
4. For each leg: resume or create an `OpportunityTracker`, `ingest()` one `LegReading` (with `event_started` computed as `now >= benchmark kickoff`, independent of feed staleness — see §6.2), and persist whatever the tracker currently holds — open or just-closed.

### 10.2 `scripts/scheduled_run.py` — the actual scheduled entry point

Each of the three sites' capture is wrapped in its own `try/except` so one scraper breaking (network hiccup, DOM change) never blocks the others or the pipeline. Sequence per cycle: capture (mode depends on which cron fired — quick / full-Swisslos / full-handicaps, see §11.1) → `run_cycle()` against both comparison sites → `force_resolve_stale()` (the `force_resolve_stale_opportunities()` safety net, §15.2 — added 2026-07-25, after this description was first written) → `auto_settle()` (ESPN) → `prune_raw_snapshots()` (+ `VACUUM` if anything was deleted). Logs to `data/logs/scheduler.log` and stdout.

### 10.3 Raw-snapshot pruning (`prune_raw_snapshots`, `vb/storage.py:546`)

Deletes `raw_market_snapshot` rows older than 24h, **except** each `(site, event_id, market_type, line)`'s single latest row, which is kept regardless of age (the live pipeline only ever reads the latest row per key). Exists because unbounded 5-minute-cadence capture reached **580k+ rows / 150+ MB in under 24h** during development — unsurvivable for unattended capture on constrained storage. `opportunity`, `opportunity_snapshot`, and `settlement` rows — the actually meaningful, non-reconstructible data — are **never** touched by this function. This is the one place in the system that deliberately deletes anything, and it is scoped as narrowly as possible in service of the standing "never lose data" directive (§11.2).

---

## 11. Infrastructure: GitHub Actions (`.github/workflows/capture.yml`)

**Why GitHub-hosted runners, not the local PC**: capture needs to keep running even when the local machine is off — this is explicitly a POC-phase decision, with VPS migration flagged as a future step (not yet started).

### 11.1 Three cron schedules, one workflow

| Schedule | What runs | Why this cadence |
|---|---|---|
| `*/5 * * * *` | Pinnacle + Loro + quick single-page Swisslos | The baseline fast cycle. |
| `7,27,47 * * * *` | Full 42-country Swisslos breadth sweep (~5 min) | Offset from the `*/5` grid on purpose so the two schedules never fire in the same minute and queue behind each other. |
| `13 3 * * *` | Full country breadth **+** Asian Handicap detail-page sweep (tens of minutes — one ~7s page load per match across hundreds of matches) | The heaviest single step by far; runs once daily rather than blocking the fast cycle, accepting stale handicap odds most of the day in exchange. |

`concurrency: { group: vb-capture, cancel-in-progress: false }` — overlapping runs queue rather than cancelling each other, so a slow handicap sweep is never killed mid-run by the next scheduled trigger. `timeout-minutes: 65` sized to comfortably exceed the handicap sweep's worst case.

### 11.2 Database persistence and the "never lose data" directive

This is the direct implementation of the user's explicit standing instruction (verbatim, from this session): *"priority no1 for this project is data collection, post process can wait and be adjusted, but never we can lose any data, make sure all data are on github, no issues due to storage size etc, verify everything before anything else."*

- The live DB (`data/vb.sqlite`) is **never committed to git** — it grows continuously and would blow past GitHub's 100 MB per-file limit within hours. Instead it's kept in **GitHub Actions cache** (`actions/cache@v4`), keyed uniquely per run (`vb-db-${{ github.run_id }}`) with a prefix-matching restore key (`vb-db-`) so each run restores the most recent prior cache entry and re-saves under its own new key — an append-then-rotate pattern, not a fixed slot.
- **The cache alone was judged insufficient** for a "never lose data" guarantee: Actions cache is LRU-evicted, has no audit trail, and no recovery path if a write silently fails. A **separate, durable daily backup** was added: `gh release upload db-sync data/vb.sqlite --clobber`, piggybacked on the once-daily `13 3 * * *` cron (not every 5 minutes — a full-DB release upload isn't free, and daily recovery-point granularity was judged sufficient for a last-resort backup). Release assets are **not** subject to cache eviction at all, so this is a meaningfully different failure mode from the cache, not just a second copy of the same risk.
- `workflow_dispatch` exposes a manual `db_sync` input (`none`/`export`/`import`) for reconciling local and GitHub-side data when they diverge — `export` uploads the current cached DB as a downloadable build artifact; `import` pulls the `db-sync` release asset and uses it going forward. Both skip the normal capture cycle for that one run.
- **Local/GitHub reconciliation tooling** (`scripts/merge_databases.py`) was purpose-built for additively merging the two independently-run capture streams (local PC + GitHub Actions) — see §14.2/§14.3 for real bugs found and fixed in this exact tool via production use.

### 11.3 Dashboard auto-deployment

Every cycle (except a bare `export`) rebuilds the dashboard from whatever's currently in the DB (`python scripts/build_dashboard.py public/index.html`) and republishes it via `actions/upload-pages-artifact@v3` + a separate `deploy-pages` job (`actions/deploy-pages@v4`), gated on the capture job's success. `public/index.html` is gitignored — self-contained, embeds all data as JSON, no git commit needed to publish. Live at **https://dexter696.github.io/value-betting-poc/**, always reflecting the last successful cycle.

---

## 12. The analysis dashboard (`scripts/build_dashboard.py` + `scripts/dashboard_template.html`)

### 12.1 What the Python side assembles

`collect_data()` (`scripts/build_dashboard.py:80`) builds one self-contained JSON blob per rebuild: every opportunity (open or closed), each with its full pre-entry history (up to 5 readings before the 3% crossing) and full in-opportunity snapshot trajectory — and for **every** point in both, the full outcome set (not just the tracked selection) for **all three books**, not just the two directly involved in that particular comparison. The third site's data is a separate fuzzy-matched lookup (`_find_matching_event_id`, reusing the exact same scorer `vb.matching` uses), cached per (site, benchmark event) so it's only paid once per match regardless of how many snapshots that match has.

### 12.2 The global entry-threshold feature — exact scope

Added per an explicit user requirement: adjust a slider and have "whole dashboard recalculate to show accurate data per % set," not just the P&L simulator panel. Deliberately scoped, per direct user instruction ("ok you can simplify and for now only use method A"), to:
- **Method A only** (Method B stays fixed at the original 3% capture threshold, used only in the static overall-comparison tiles).
- **Range 3%–15% only** — going *below* 3% would need the shorter, capped pre-entry history rather than the full in-opportunity trajectory, and was explicitly scoped out.

Mechanically (`scripts/dashboard_template.html:340-390`): the stored `entryEdgeA`/`entryComparisonOdds`/`bucket` fields on each opportunity are fixed to the *original* 3% crossing — that's what made it a tracked opportunity at all. `effectiveEntryA(opp, threshold)` instead scans that opportunity's own already-captured snapshot trajectory for the first point where `edgeA >= threshold`, using that real recorded point's actual odds as the simulated entry — **no re-scrape, no synthetic data**, purely a re-read of already-stored history. An opportunity that never reached the chosen threshold returns `null` and is excluded from every downstream tile/row/simulator computation at that threshold (`qualifiesAtThreshold`) — exactly mirroring what "we'd only have bet above X%" means. Results are cached per threshold value (`entryCache`) since threshold changes far less often than other filters.

The "Entry B (3%)" column was **removed** after this feature shipped: once Entry A could move independently via the slider while Entry B stayed pinned at 3%, showing them side-by-side in the same row stopped being a fair, like-for-like comparison — explicit user call ("we agreed to remove it right for now?").

### 12.3 The converge-time filter (`ce7651b`, fixed `1e25ce5`)

A second slider, same UI pattern and same *reach* as the entry threshold (recalculates tiles, table, and simulator, not just the table) — but filtering on `convergence_time` (§6.3) instead of edge magnitude: "only count opportunities whose observed above-threshold window lasted at least N minutes." Explicit user request, with an explicit starting value ("starting with 5m higher, so I can say filter whatever is below 20m of converge").

Mechanically (`scripts/dashboard_template.html`, `qualifiesAtConvergence()`): still-open opportunities have `convergenceSeconds === null` (their eventual duration isn't known yet) and never pass a nonzero filter, mirroring how `qualifiesAtThreshold()` already treats "never reached" opportunities — same "can't include what we don't know yet" logic applied to a different field. Applies to **both** Method A and Method B tiles (unlike the entry threshold, which is Method-A-only) — deliberately, since converge time isn't a method-specific quantity, it's the same real observed window for both methods on a given opportunity.

**Slider range is 0–180 (step 5), default 5** — the 0 ("off") position is load-bearing, not decorative: it's the *only* way to see still-open opportunities or fast-converging settled bets at all, since every position above 0 excludes them by construction. This was originally shipped with `min="5"` (no reachable 0), a real regression caught by the self-audit in §14.6 — dragging to 0 now exactly reproduces the pre-filter tile values (verified in-browser: 169/112/57 bets-identified/awaiting/settled, 95 resolved, matching the numbers from before this feature existed).

### 12.4 The match-table Odds column (`a9fe263`)

A dedicated "Odds" column between "Match" and "Bucket" in the opportunity table, showing `entry.comparisonOdds` — the **comparison site's** price at entry, i.e. what would actually have been bet, as distinct from Pinnacle's reference price (which set the *edge*, not the *stake payout*). Previously this was only visible by expanding a row's detail panel; explicit user request to surface it directly in the table. Sortable like every other column (`sortValue()`'s `'betOdds'` case).

### 12.5 P&L simulator

Flat-vs-fractional-Kelly staking math is **ported 1:1** from `vb/evaluation.py` into JavaScript (`scripts/dashboard_template.html:317-338`) — not reimplemented independently — specifically so the two are guaranteed to agree rather than risk silent drift between a Python "source of truth" and a JS "display copy." A visible formula note (`#kellyFormula`) was added after a user misconception was clarified during development: **Kelly fraction = 1.0 is *not* equivalent to flat staking** — full Kelly still varies stake per bet based on that bet's own edge and odds, unlike flat staking's fixed size; only lower fractions scale every bet's stake down *proportionally*, they don't collapse to a fixed amount.

### 12.6 Tile groups

Three groups, restructured mid-session for clarity: **Data Collection** (raw capture volume, independent of any threshold), **Betting Activity** (bets identified / awaiting result / settled, all *at the current entry threshold AND converge-time filter* — §12.3 — a secondary, visually muted tile shows raw "resolved" count for context, since resolved-vs-settled is a real distinction per §6.2 that's easy to conflate; at the converge filter's 0/"off" position this tile is a meaningfully smaller subset of "bets identified," at any nonzero position it becomes identical to it since a nonzero converge filter already excludes every still-open opportunity — expected, not a bug, see §14.6), and **Performance** (Method A at the current threshold vs. Method B fixed at 3%, side by side — both also respect the converge-time filter).

---

## 13. Data integrity tooling and the "never lose data" audit trail

Per the standing directive quoted in §11.2, three purpose-built tools exist specifically to prevent silent data loss when local and GitHub-side databases (two independently-running capture streams) diverge:

- **`scripts/merge_databases.py`**: additive merge, dedupes on each table's *real* natural key (never a raw autoincrement id, which has no cross-database meaning). `opportunity.instance_id` is the hard case: deterministic within one DB, not guaranteed unique across two independently-run ones. A same-`instance_id` collision is only renamed-and-reinserted (preserving both sides' data) if the incoming row's **core fields actually differ** from what's already present; if they're byte-identical, it's recognized as shared-ancestry and skipped outright.
- **`scripts/dedupe_after_bad_merge.py`**: one-off cleanup for data already corrupted by a real bug in the merge tool before it was fixed (§14.2) — removes only exact duplicates, safe to re-run.
- **GitHub release backup** (§11.2): durable, cache-eviction-immune daily snapshot.

---

## 14. Known bugs found and fixed this development cycle (full audit trail)

Documented here in the interest of full transparency for audit, per the user's explicit "attention to detail" request — these are real production bugs that shipped, were caught via direct investigation of user-reported anomalies (never assumption), root-caused, fixed, and verified against live data.

### 14.1 Convergence-time truthiness bug (`d736e43`)

**Symptom**: user reported specific settled matches (Besiktas vs Midtjylland, FC Twente vs Ferencvaros, Varazdin vs Jablonec, and others) showing "—" (no data) for convergence time despite being confirmed settled.

**Root cause**: `scripts/build_dashboard.py` computed `"convergenceSeconds": opp.convergence_time.total_seconds() if opp.convergence_time else None`. `opp.convergence_time` returns a `timedelta` — and **`timedelta(0)` is falsy in Python**. An opportunity whose entry and resolution landed in the *same* capture reading (e.g. kickoff fell inside a single 5-minute gap between cycles, so the very first reading that ever crossed 3% also already showed `event_started`) produces a real, valid `timedelta(0)` — but the truthiness check silently converted that into "no data" instead of "0 minutes."

**Fix**: changed to `if opp.convergence_time is not None else None` (`scripts/build_dashboard.py:174`).

**Verified impact**: **17 of 75 resolved opportunities (23%)** in the live dataset were affected at time of fix. The rest of the codebase (Python and the dashboard's JS) was checked for the same pattern; no other instance was found — every other `None`-check in the codebase operates on enums, datetimes, or tuples, none of which have a falsy-but-valid state, or uses explicit `!== null` in JS.

### 14.2 Database merge duplication bugs (`6318535`)

**Trigger**: user's explicit instruction, when asked whether pruning before merging was acceptable to reduce storage risk: *"no we do not want to lose any data, we need to add missing data to github and analyze properly"* — ruling out any shortcut that could drop real data, which forced building the merge tool correctly rather than around the problem.

**Two independent root causes found via actual production use** (dogfooding the tool against the real diverged local/GitHub databases):

1. **Opportunity collision handling** treated *any* same-`instance_id` match between the two databases as a genuine collision and always renamed+reinserted it — without first checking whether the incoming data was byte-identical to what was already present. Two databases that share ancestry (both descend from an earlier sync) legitimately have many identical `instance_id`s; treating those as fresh collisions duplicated **~140 opportunity rows** on the first real run.
2. **Settlement merge used `INSERT OR IGNORE`**, relying on the table's `UNIQUE` constraint to silently no-op on a true duplicate. But `line` is `NULL` for every match-winner settlement (the majority of rows), and SQLite's `UNIQUE` constraint — like standard SQL — never treats two `NULL`s as equal, so every NULL-line row was treated as "new" and duplicated **16 settlement rows**.

**Fix**: opportunity merge now compares full core-field tuples before deciding rename-vs-skip (§13); settlement merge replaced with an explicit `NULL`-safe existence check (`line IS ? OR line = ?`) mirroring the fix already present in `vb.storage.save_settlement()` (§9) — the same NULL-uniqueness gotcha had to be re-discovered and independently re-applied here because a raw SQL merge query doesn't go through that function.

**Cleanup**: `scripts/dedupe_after_bad_merge.py` was written and run against the already-corrupted local DB — removed 109 duplicate opportunities, 16 duplicate settlements, 414 orphaned snapshot rows. Verified via synthetic collision tests (both before and after the fix, confirming the fix actually changes behavior) and then a full check that the real local DB was clean (0 duplicate groups) before re-syncing to GitHub.

### 14.3 Deeper opportunity-duplication bug in `merge_databases.py` (`5ccfb08`, 2026-07-25)

**Symptom**: user-reported screenshot showing 4 dashboard rows for "Al-Ettifaq vs York City" that looked like near-duplicates (two pairs of rows with matching edge numbers and identical "2h58min" duration).

**Root cause**: this was a second, deeper bug in the same tool fixed in §14.2 — the 2026-07-24 fix stopped renaming *byte-identical* rows, but the collision check still compared the **full** opportunity core tuple, including `resolved_at`/`resolution_reason`. Those two fields legitimately change over an opportunity's own lifetime (null while open, set once it closes) — so when two independent capture streams (local PC + GitHub Actions) each tracked the *same* real opportunity but observed it at different points (one before it closed, one after; or via a different `resolution_reason` because of slightly different capture cadence timing), the tuples differed and the merge treated them as genuinely different opportunities, renaming and duplicating them instead of recognizing them as one.

**Verified scope**: querying the live database directly for groups sharing the same `(market_key, first_cross_at)` — the actual stable identity, since `first_cross_at` is set once at creation and never changes, unlike `instance_id`'s `#N` suffix, which is only a per-database bookkeeping number — found **35 duplicate groups / 41 excess rows out of 209 total opportunities (≈20%)**, spanning many matches, not just the one in the report.

**Fix**: added `reconcile_opportunity_groups()` to `merge_databases.py`, run unconditionally at the end of every `merge()` call as a final invariant ("at most one row per `(market_key, first_cross_at)`"), plus a standalone `scripts/reconcile_opportunities.py` for cleaning already-corrupted data directly. Collapsing a group keeps the most objectively-complete survivor (`event_started` outranks any other resolution, since kickoff time is a deterministic wall-clock fact rather than something sampling-dependent; otherwise more snapshots wins) and **unions every group member's snapshots into it** (deduped by `captured_at`) so no captured data is lost in the collapse — only the artificial extra `instance_id`. Verified against a synthetic reproduction of the exact fragmentation pattern, then run against the live database: 35/35 groups collapsed, 8 genuinely-missing snapshots recovered from the losing instances, 0 duplicate groups remaining afterward. Full test suite green (141/141) before pushing the cleaned data live.

### 14.4 ESPN auto-settle coverage gaps (`bd0fd49`, 2026-07-25)

Found while manually clearing a 15-match settlement backlog (see §15.2's resolution below): Switzerland was **entirely absent** from `COUNTRY_TO_ESPN_CODE` (no country code, no Super League/Challenge League tier-name mapping) despite Swisslos being one of only two comparison sites; Argentina's top flight is listed by Pinnacle as "Liga Pro" but only "Liga Profesional" was recognized (a pure naming mismatch, not a coverage gap); Nordic club-suffix tokens ("IF"/"IS") weren't stripped by `normalize_team_name()`, so a name that ESPN actually had data for ("Örgryte IS" vs Pinnacle's "Orgryte") scored 82% similarity — just under the 88% safety bar — and was correctly rejected rather than risk a wrong settlement; and "Club Friendlies" was silently routed to the international-friendlies ESPN slug (`fifa.friendly`), which was verified live to carry **zero** club fixtures, meaning every pre-season club friendly failed auto-settlement every single cycle, forever, with no way for that to ever change.

Fixed the first three (added Switzerland's country code + both tier names, added the "Liga Pro" alias, added "IF"/"IS" to the shared stopword list) — verified directly against the actual matches that had failed. The fourth (club friendlies) has no cheap fix: ESPN doesn't expose pre-season club friendlies under one enumerable slug at all (each is its own small one-off tournament) — this is now excluded explicitly in `_espn_slug_for()` so it reads as "checked, can't place it" rather than silently trying the wrong slug forever; it remains a permanent, correctly-documented gap requiring manual/web-search settlement (see §15.3).

### 14.5 Reports investigated and found NOT to be bugs

Included for completeness — the pattern in this project has been to verify every anomaly report against real live data rather than assume, and not every report turns out to be a real defect:

- **"Duplicate bets"** (Hapoel Tel Aviv vs Ludogorets Razgrad, Paksi vs Panathinaikos, and others): investigated by pulling the actual live GitHub-hosted database (via `db_sync=export`, not local data or assumption) and tracing exact instance-level detail. Confirmed these are **legitimate separate opportunities** — different selections and/or different comparison sites tracked independently for the same match, not duplication.

### 14.6 Formal self-audit of this session's own work (`1e25ce5`, 2026-07-25)

Distinct from every other entry in this section: not a user-reported anomaly, but a proactively-run, structured code review (high-effort: 8 independent finder angles — 3 correctness, 3 cleanup, altitude, CLAUDE.md conventions — each surfacing up to 6 candidates, followed by an independent one-vote verifier per surviving candidate) against the full cumulative diff of this session's own work (`d736e43..HEAD` at the time, ~930 lines across 10 files: `merge_databases.py`, `reconcile_opportunities.py`, `scheduled_run.py`, `vb/normalize.py`, `vb/sources/results.py`, `vb/storage.py`, plus tests and docs). Run specifically because the user asked to "learn another skill to audit yourself properly."

**Process integrity note**: this section documents the audit's own findings, including bugs in features shipped *earlier in the same session* (the converge-time filter, §12.3) — the point of running a self-audit is precisely to catch this category of thing rather than only ever checking work retroactively when a user notices something.

Of ~9 candidate findings that survived the 8 finder angles, verification (Phase 2, one independent agent per candidate) split them as follows:

**Confirmed and fixed:**
1. **Converge-time filter had no reachable "off" position** (§12.3) — slider minimum was 5, not 0, so the filter silently excluded every still-open opportunity from the *entire* dashboard by default with no way to see the unfiltered picture, and made the "resolved" tile mathematically identical to "bets identified" (§12.6) while "awaiting result" silently stopped counting live/in-flight bets. Fixed: slider now reaches 0 ("off"); verified in-browser to exactly restore pre-feature tile values.
2. **`merge_databases.py`'s docstring was factually false** — it claimed "never deletes or overwrites existing row's data," directly contradicted by the `reconcile_opportunity_groups()`/`reconcile_snapshot_duplicates()` passes added earlier the same day (§14.3, §15.1), which do delete rows, including pre-existing duplicate groups in the destination DB unrelated to the current merge call. A future reader trusting the stale claim could call `merge()` speculatively without a backup. Docstring corrected to state the real, narrower guarantee precisely.
3. **`force_resolve_stale_opportunities` (§15.2) can leave a stale trajectory** on a force-resolved opportunity — confirmed via direct code trace (not just plausible): a normal close always appends a final snapshot at the exact closing moment, but force-resolve deliberately never touches `opportunity_snapshot` (to avoid fabricating an odds reading that was never observed), so `peak_edge_a`/`convergence_time` can silently reflect a reading that's stale by however long the opportunity sat undetected. Accepted as the correct tradeoff versus fabricating data (documented precisely in §15.2 rather than silently assumed away).

**Plausible, not fixed as code (documented instead)**: `reconcile_opportunity_groups()`'s survivor-selection ranks resolution status above snapshot completeness, so a duplicate group's final `resolved_at`/`resolution_reason` can come from a sparser, weakly-resolved entry even though the richer trajectory survives via snapshot union (no data lost, but the metadata label can be wrong) — left as-is since resolving it correctly requires knowing which of two divergent capture streams is more "caught up," which isn't decidable from the data alone. Also: the 2026-07-25 addition of "if"/"is" to the shared team-name stopword list (§14.4) feeds pre-bet cross-site matching (`vb.matching`) in addition to its intended ESPN-settlement target, with no concrete colliding team pair found or ruled out — closed the *test coverage* gap (`vb/tests/test_normalize.py`, two new tests) without reverting or complicating the underlying fix, since the motivating miss was real and the fix correctly closed it.

**Investigated and refuted, with reasoning** (i.e. the audit process working as intended — not everything a finder angle surfaces is real): a proposed race between `force_resolve_stale_opportunities` and a concurrent `save_opportunity()` call was refuted by tracing the actual deployment model (single-threaded `scheduled_run.py`, `concurrency: group: vb-capture` locking GitHub Actions to one run at a time, no shared live connection between local and cloud databases — the race has no reachable trigger); a proposed snapshot-dedup tiebreak edge case was refuted by finding the exact guard in `reconcile_opportunity_groups()` that already prevents the precondition from ever occurring; an N+1 query pattern in `force_resolve_stale_opportunities` was real but refuted on severity (sub-millisecond indexed point-reads, dwarfed by the minutes-long browser-scraping cost of the same cycle); and a concern about the "Club Friendlies" ESPN-exclusion string match was refuted directly against live data (`SELECT DISTINCT competition ... LIKE '%riendl%'` confirmed the real Pinnacle string is exactly `'Club Friendlies'`/`'Club Friendlies Women'` with no prefix, matching the code's assumption).

---

## 15. Known limitations and open items (as of this document's writing)

These are real, currently-unresolved items — listed explicitly rather than omitted, per the audit framing.

### 15.1 Same-timestamp multi-edge snapshots — RESOLVED (data), root cause still not fully pinned (`47c339a`, 2026-07-25)

Was: specific `dropped_below_threshold` opportunities showing multiple snapshot rows sharing the *exact same* `captured_at` timestamp within one single instance — a different pattern than §14.3 (that was duplicate *opportunity* rows across different `instance_id`s; this was duplicate *snapshot* rows within one). Confirmed the duplicate rows weren't byte-identical — `edge_a`/`benchmark_odds`/`comparison_odds` matched exactly across a group, but `edge_b` drifted by small amounts, only possible if they came from genuinely different real capture moments. Investigated the original hypothesis (an older, buggier version of the `ingest()` timestamp-dedup guard) directly against git history — `vb/opportunity.py`'s guard has been byte-identical since the initial commit, which **rules that out**. Most likely explanation is a residual of `merge_databases.py`'s pre-`6318535` behavior (before that fix, every source opportunity row was unconditionally inserted with no identity check at all), but this is not confirmed with the same confidence as §14.3's diagnosis.

**Fixed as data regardless of the open root-cause question**: `reconcile_snapshot_duplicates()` added to `merge_databases.py` (same "run unconditionally as a final invariant" pattern as §14.3), collapsing every `(opportunity_instance_id, captured_at)` group to one row. Verified scope was wider than the 5 originally-flagged instances: **32 groups / 42 excess rows** across the live database. Cleaned and pushed; 0 remaining. If this recurs after today, the merge-tool hypothesis is wrong and it needs fresh investigation — worth checking first whether it's still happening at all now that both merge-tool bugs (§14.3 and this one) are fixed.

**Update, 2026-07-25 (same day, later)**: it recurred — 2 new duplicate-snapshot groups found in a fresh pull of live data that had **not** gone through any merge operation (a plain `db_sync=export` of the GitHub Actions cache, capture-only). This is real evidence against the "purely a `merge_databases.py` pre-`6318535` residual" hypothesis: if the merge tool were the only source, a non-merged export should have shown zero. At this scale (2 rows out of 656,354 total snapshots) it's not remotely a data-integrity concern and the standing `reconcile_snapshot_duplicates()` fix cleans it automatically whenever it runs — but the root cause is now genuinely open again, not just unconfirmed. Worth investigating properly if it keeps recurring at a growing rate rather than staying at this trace level.

### 15.2 Opportunities stuck open past kickoff+3h with no settlement — RESOLVED 2026-07-25 (both backlog and mechanism)

Was: five opportunities (all Al-Ettifaq vs York City, plus Royal Antwerp vs Olympiakos) stuck `is_open=True` well past kickoff+3h with no settlement, per the residual gap described in §6.2 (Pinnacle stops sending readings entirely once a match goes live, so the tracker never receives the reading that would let it close). The day's backlog was cleared by settling manually (§14.4). The underlying *mechanism* gap is now also fixed (`f05211a`): `vb.storage.force_resolve_stale_opportunities()` runs every scheduled cycle (`scripts/scheduled_run.py`), force-closing any opportunity still `is_open=True` more than 4h past its own benchmark kickoff — `resolved_at` set to the kickoff time itself (matching the normal `EVENT_STARTED` close convention) and only the opportunity header touched, never the already-captured snapshot trajectory. 3 new tests (`vb/tests/test_storage.py`); applied against live data first (found 0 currently-stale — today's earlier reconciliation work had already cleared everything), so this is a forward-looking guard rather than a further backlog cleanup. Note this doesn't explain *why* the normal close path stopped firing for those five in the first place (still not root-caused with certainty — see §6.2's own hypothesis) — it guarantees the symptom self-heals within one cycle going forward regardless of cause.

**Known, accepted precision gap of this fix (surfaced by code review, 2026-07-25)**: a *normal* close (`OpportunityTracker._close`) always appends one final snapshot at the exact closing moment, so `snapshots[-1].captured_at == resolved_at` for every naturally-closed opportunity. `force_resolve_stale_opportunities` deliberately does **not** fabricate a matching final snapshot (doing so would mean inventing an odds reading that was never actually observed, which conflicts with this project's core "capture is real, never fabricated" discipline — see §1) — it only sets the header's `resolved_at`/`resolution_reason`. This means a force-resolved opportunity's `peak_edge_a`/`peak_snapshot`/`convergence_time` (§6.3) reflect whatever the last *real* reading happened to be, which can be hours stale relative to `resolved_at` — and nothing in the data distinguishes a force-resolved row from a normal one, so this staleness is invisible to any downstream consumer (`vb/reporting.py`, the dashboard's `peakEdgeA`/`convergenceSeconds` fields). Accepted as the correct tradeoff (fabricating data would be worse), but worth knowing when interpreting `peak_edge_a` for an opportunity that turns out to have been force-resolved.

### 15.3 ESPN coverage gaps

Seven countries Swisslos covers have confirmed zero ESPN league coverage (Poland, Croatia, Slovakia, Slovenia, South Korea, Hungary, Bulgaria — `KNOWN_UNCOVERED_COUNTRIES`, §7.2) and fall back to manual settlement permanently, by design, not as a temporary gap. Switzerland's own coverage was fixed 2026-07-25 (§14.4); pre-season "Club Friendlies" remain permanently uncovered (no enumerable ESPN slug exists for them) and always will need the manual path.

**Standing mitigation (added 2026-07-25)**: since GitHub Actions can't run web searches, anything ESPN permanently can't reach will always need a human/LLM-assisted settlement pass. A daily automated sweep (session-bound Claude Code cron job, not GitHub Actions — auto-expires after 7 days and needs re-scheduling if the session ends) pulls the live DB, lets ESPN catch whatever it now can, web-searches the rest, records results, and pushes back — so this backlog is capped at ~24h going forward instead of accumulating indefinitely until someone notices.

### 15.4 Pinnacle's in-play event-id split

Documented in §3.1 as a confirmed, understood quirk of Pinnacle's own data, not a bug in this system — flagged here again because it does mean `raw_event`/`raw_market_snapshot` row counts for a single real match can legitimately be higher than 1:1, which could read as "duplication" to someone unfamiliar with this quirk.

### 15.5 Method B is not yet entry-threshold-adjustable in the dashboard

Explicitly deferred (not cancelled) during the threshold-slider feature build — Method B's dashboard figures remain fixed at the original 3% *entry-edge* capture threshold; only Method A responds to the entry-threshold slider (§12.2). This is unrelated to the separate converge-time filter (§12.3), which **does** apply to both methods since converge time isn't method-specific.

### 15.6 Infrastructure is POC-grade

Runs entirely on GitHub-hosted Actions runners rather than a persistent VPS — chosen so capture continues when the local PC is off, at the cost of the 65-minute job timeout and cron-based (not truly continuous) scheduling. This is also the direct architectural cause of both duplication bugs fixed 2026-07-25 (§14.3/§14.4): the ephemeral, cache-based database plus the local PC historically running its own independent `scheduled_run.py` created exactly the "two divergent capture streams" precondition those bugs needed to occur. A migration research/draft plan was written 2026-07-25 (`VPS_MIGRATION_PLAN.md`) — research only, nothing executed; its own recommendation is to let today's fixes prove themselves for 1-2 weeks before taking on the migration itself.

---

## 16. Testing

146 tests across 16 files (`vb/tests/test_*.py`) as of `a9fe263`, covering: edge calculation, evaluation/staking math, Loro/Swisslos/Pinnacle scraper parsing, cross-site matching (including the swap-suspicion and time-cutoff cases), name normalization (including the two Nordic-club-suffix regression tests added by the self-audit, §14.6), the opportunity lifecycle state machine, the full pipeline (`run_cycle`), raw-snapshot pruning, raw-capture storage (including the force-resolve-stale-opportunities safety net), reporting/pre-entry history reconstruction, ESPN result-matching, the human review queue, settlement math (including quarter-line handicap splitting), and settlement storage (including the NULL-line dedup behavior). Run via `pytest` from the project root.

---

## 17. File map

```
vb/
  models.py        Core dataclasses: RawEvent, Outcome, MarketSnapshot, MarketType, Selection enums
  normalize.py      Team/competition name normalization, handicap-line canonicalization
  matching.py       Cross-site event + market matching, AUTO/REVIEW/REJECT tiers
  edge.py           Method A (raw_edge) and Method B (devigged_edge) calculation
  opportunity.py    Opportunity lifecycle state machine (OpportunityTracker)
  settlement.py     Final-score -> WON/LOST/PUSH/HALF_WON/HALF_LOST
  storage.py        SQLite persistence layer for everything above
  pipeline.py       Wires matching + edge + opportunity tracking together (run_cycle)
  reporting.py      Read-only report assembly (finished matches, pre-entry history)
  evaluation.py     Method A vs B comparison, bucketed, flat + Kelly ROI
  schema.sql        SQLite schema (5 tables)
  sources/
    pinnacle.py      Benchmark scraper (guest API)
    swisslos.py       Comparison scraper #1 (Playwright)
    loro.py            Comparison scraper #2 (Playwright)
    results.py         ESPN auto-settlement
  tests/             146 tests, one file per module above

scripts/
  scheduled_run.py         Full capture+pipeline+force-resolve+settle+prune cycle (the scheduled entry point)
  capture_pinnacle.py, capture_swisslos.py, capture_loro.py   Standalone single-site capture
  run_pipeline.py          Standalone pipeline-only run
  auto_settle.py           Standalone ESPN settlement run
  record_result.py         Manual settlement entry
  review_queue.py, review_ui.py, review_ui_template.html      Human review of REVIEW-tier matches
  build_dashboard.py, dashboard_template.html                 Analysis dashboard build
  evaluation_report.py, finished_matches_report.py            CLI evaluation/reporting output
  merge_databases.py       Additive local/GitHub DB reconciliation + duplicate-group reconciliation (§14.3/§14.4)
  reconcile_opportunities.py  Standalone runner for the same reconciliation passes - safe to run as a periodic health check
  dedupe_after_bad_merge.py   One-off cleanup for the original merge bug (§14.2)

.github/workflows/capture.yml   3-schedule capture pipeline, dashboard deploy, DB backup
data/vb.sqlite                  Live database (gitignored; lives in Actions cache + release backup)
VB - methodology.docx           Original methodology document
VB - methodology - addendum.md  Opportunity-lifecycle model addendum (2026-07-23)
PROJECT_DOCUMENTATION.md        This document
VPS_MIGRATION_PLAN.md           Infrastructure migration research/draft (2026-07-25) - not executed
```

---

## 18. Glossary

- **Benchmark**: Pinnacle.com, treated as the reference "true" price.
- **Comparison site**: Swisslos.ch or Loro.ch — the soft book being checked for mispricing against the benchmark.
- **Edge**: expected value of a bet, as a fraction (0.05 = 5%), computed against a probability estimate — Method A's or Method B's.
- **Leg**: one specific `(match, market_type, line, selection)` combination — the atomic unit tracked and settled.
- **`market_key`**: stable identifier for one leg vs. one comparison site, shared across re-occurring `Opportunity` instances.
- **`instance_id`**: unique identifier for one continuous above-threshold period (`"{market_key}#{seq}"`).
- **De-vig**: removing a bookmaker's built-in margin from its quoted odds to recover a fair (sum-to-1.0) probability estimate.
- **Overround**: a market's total margin — how much implied probabilities across all outcomes sum to over 1.0.
- **Quarter-Kelly**: staking at 25% of the full Kelly-optimal fraction, to reduce variance relative to full Kelly.
