# Remediation response to the 2026-07-25 audit

**Written for**: the auditor's second-pass review.
**Written against**: commit `6258509`, database `db-sync` release last updated 2026-07-29T13:01:51Z.
**Companion document**: `PROJECT_DOCUMENTATION.md` §0 covers the same ground organized by the audit's own 8-phase roadmap; this document goes finding-by-finding (all 21) instead, for direct cross-reference against `value-betting-system-audit-2026-07-25.md`.

**How to read the status column**: **FIXED** means verified against current code and, where the finding touches the live pipeline, verified against a real production run (`gh workflow run capture.yml`, watched to completion, logs grepped for errors). **PARTIALLY FIXED** means real, verified progress but a real gap remains — the gap is stated explicitly, not glossed over. **NOT FIXED** means still open, with the reason stated. Every claim below names the file/function responsible so it can be independently re-checked rather than taken on faith.

---

## Executive summary

Of the 21 findings, **14 are fixed and verified live**, **5 are partially fixed with the remaining gap stated explicitly** (F-04, F-06, F-11, F-12/F-13, F-20), and **2 remain open** (F-07, F-21) — both for the same underlying reason: they require the VPS migration or are scoped to the legacy v1 dashboard path, not more isolated code changes.

**Two items were built to real completion and then deliberately not activated, by the project owner's explicit decision, not a technical blocker:**

- **A Betfair Exchange client** (`vb/sources/betfair.py`) — a real, tested implementation against Betfair's actual API. **Not being pursued further right now.** Blocked on the project owner creating a real account (something outside what the assistant will do regardless of authorization) and a design decision on whether it's a second benchmark or a comparison book.
- **The VPS migration** (Phase 2) — fully researched and documented (`VPS_MIGRATION_PLAN.md`), reviewed by the project owner, and explicitly declined on 2026-07-29 in favor of staying on GitHub Actions' free tier. **Not being pursued further right now.**

Both are recorded as deliberate decisions, not gaps, and are the direct cause of F-07 and part of F-11 staying open.

---

## Finding-by-finding status

| # | Finding (translated) | Status |
|---|---|---|
| F-01 | Pipeline compares arbitrarily old odds | **FIXED** |
| F-02 | Tracker restart can overwrite an old opportunity | **FIXED** |
| F-03 | `event_started` stores the wrong time and a stale reading | **FIXED** (v2 only — see note) |
| F-04 | Method B doesn't search for its own crossing | **PARTIALLY FIXED** — mechanism fixed, Method B not actually running |
| F-05 | `convergence` is a look-ahead filter | **FIXED** |
| F-06 | Results didn't originate from audited code | **PARTIALLY FIXED** |
| F-07 | GitHub Actions cadence doesn't match the methodology | **NOT FIXED** — needs the declined VPS migration |
| F-08 | Daily handicap capture got lost | **FIXED** |
| F-09 | Overlapping instances and missing bet identity | **FIXED** |
| F-10 | Pruning picks the highest ID instead of the newest time | **FIXED** |
| F-11 | Merge isn't lossless or fully idempotent | **PARTIALLY FIXED** — 3 of 8 documented sub-cases closed |
| F-12 | Method A doesn't measure fair expected value | **PARTIALLY FIXED** — infrastructure exists, not used by the frozen confirmatory protocol |
| F-13 | Proportional de-vig doesn't address favorite-longshot bias | **FIXED** (at infrastructure level — see F-12 note) |
| F-14 | Greedy matching is order-dependent, orientation isn't in the data | **FIXED** |
| F-15 | An opportunity isn't a bet and execution is missing | **FIXED** |
| F-16 | Scraper/pipeline failure doesn't fail the workflow | **FIXED** |
| F-17 | Settlement arithmetic is fine, evidence isn't enough | **FIXED** |
| F-18 | Kelly is a sizing hypothesis, not proof of edge | **FIXED** |
| F-19 | Invalid variants can pass through as an implicit "other option" | **FIXED** |
| F-20 | Build isn't exactly reproducible | **PARTIALLY FIXED** |
| F-21 | Pre-entry chart isn't a historical replay of the decision | **NOT FIXED** — v1-legacy scope only |

---

## Detail on every non-trivial status

### F-01 — FIXED
`vb/freshness.py`'s `check_freshness()` rejects a benchmark/comparison pair as ineligible if either side is stale (`max_age_s`) or the two are mutually skewed (`max_skew_s`), before the pair can open or extend an episode. Wired into `vb/pipeline.py::run_cycle_v2` via `LegReadingV2.eligible`/`reject_reason`. A rejected pair is still recorded as an auditable `signal_observation` (never silently dropped), it just never opens/extends an episode.

### F-02 — FIXED
`vb/identity.py::new_id()` mints a UUID at true creation time, never a process-local counter. `vb/episode.py::EpisodeTracker` carries no in-memory state across calls — every `ingest()` re-derives the current open episode fresh from the database (`find_open_signal_episode`). Regression test: `vb/tests/test_episode.py::test_f02_reproduction_through_the_real_tracker_class` constructs two independent tracker instances (standing in for two separate processes) and proves they never collide.

### F-03 — FIXED (v2 only)
Fixed 2026-07-29 in `vb/episode.py::EpisodeTracker.ingest()`. `LegReadingV2` now carries `now` (process wall-clock time) and `kickoff_utc` separately from `received_at` (the last real odds observation's own timestamp). An `event_started` close now stamps `ended_at = max(now, kickoff_utc)` — the audit's own precise formula — instead of the stale reading's `received_at`. A close with no new snapshot since the last recorded observation reuses that observation's real id (looked up by `(episode_id, benchmark_snapshot_id, comparison_snapshot_id, edge_model)`) instead of fabricating a duplicate row.

**Explicit scope note**: v1's `vb/opportunity.py::OpportunityTracker._close()` has the identical bug and was deliberately left unfixed — it's the producer of the frozen legacy dataset (Phase 0), and per that phase's own discipline, the legacy path isn't patched incrementally. All correctness investment goes to v2. `vb/opportunity.py` continues to run unchanged, alongside v2, as of this writing.

### F-04 — PARTIALLY FIXED
The structural bug is fixed by construction: `run_cycle_v2(conn, benchmark_site, comparison_site, strategy, ..., edge_selector)` takes a `StrategyDefinition` and an `edge_selector` together, and drives a fully independent `EpisodeTracker` keyed by that strategy's own `strategy_version` — calling it once with a Method-A `StrategyDefinition`/`edge_a` selector and again with a Method-B `StrategyDefinition`/`edge_b` selector gives Method B its own independent scan for its own first crossing, not inherited state from Method A. This is real and tested.

**What's not done**: `scripts/scheduled_run.py::run_pipeline_v2()` currently only ever constructs and runs the Method-A `StrategyDefinition` — Method B is not instantiated as a standing cohort in the live shadow pipeline. The mechanism that would fix F-04 in production is built and correct; it isn't actually invoked for Method B yet. Confirmed by direct grep of `scheduled_run.py`: no Method-B `StrategyDefinition` construction exists there.

### F-05 — FIXED
`vb/strategy.py`'s `ImmediateEntryPolicy`/`PersistentEntryPolicy` are online state machines that only ever look at observations with `decision_time <= as_of` — no access to future data. Wired live via `vb/decision_runner.py`. This replaces the retrospective convergence-time filter the audit examined, which by definition could only be computed after knowing how long an opportunity's window stayed open — a look-ahead by construction.

### F-06 — PARTIALLY FIXED
`EvaluationRun` (`vb/models.py`) carries `code_sha`/`config_hash`/`db_snapshot_hash`/`data_cutoff`. `vb/evaluation_runner.py::run_evaluation()` assembles real `ExecutedBet` rows from actual `bet_decision`/`bet_execution`/`signal_episode`/`settlement_version`/`closing_snapshot` data — not synthetic fixtures — and produces a real report via `vb/evaluation_v2.py::build_report()`. As of 2026-07-29 this is wired into the live daily cron (`scripts/scheduled_run.py::run_daily_evaluation()`, gated on the existing `--full-handicaps` low-frequency trigger), generating a real `evaluation_run` row once daily. Before this it was built and tested but never invoked outside one-off manual runs.

**What's not done**: the *dashboard* (`scripts/build_dashboard.py`) still reads from the old v1 `vb/evaluation.py` path over v1 `opportunity` data, not the new auditable `evaluation_v2` pipeline. This is intentional, not an oversight — v1's dashboard is the explicitly-labeled legacy path (Phase 0), and there is no live-rendered report of the v2 `evaluation_run` data yet. The data itself is real and auditable; a UI for it is a separate, not-yet-done piece of work.

### F-07 — NOT FIXED
`.github/workflows/capture.yml` retains the same three overlapping cron schedules with `concurrency: { group: vb-capture, cancel-in-progress: false }` the audit examined — unchanged. `VPS_MIGRATION_PLAN.md` was written 2026-07-25 and reviewed by the project owner 2026-07-29, who explicitly chose to remain on GitHub Actions rather than migrate. This finding's fix is the migration itself; since that's declined, F-07 stays open by direct consequence, not oversight.

### F-08 — FIXED
`.github/workflows/capture.yml`'s `capture` job gained `permissions: contents: write` at the job level (previously inherited the repo default, read-only), which was causing the daily backup step to fail with `HTTP 403`. Fixed and verified live 2026-07-25.

### F-09 — FIXED
`BetDecision.idempotency_key` (`vb/models.py`, `UNIQUE` constraint) is computed in `vb/execution.py` from `(strategy_version, market_identity_id)` — enforces "at most one decision per strategy/market" at the database level, not by convention.

### F-10 — FIXED
`vb/storage.py::prune_raw_snapshots()` ranks "latest" by `captured_at`, not `MAX(id)` — an id ordering a merge from two independent databases can violate.

### F-11 — PARTIALLY FIXED (3 of 8 documented sub-cases)
The audit's own F-11 section lists 8 specific sub-cases (`value-betting-system-audit-2026-07-25.md` lines 1336–1348). Status of each, checked directly against `scripts/merge_databases.py` as of this writing:

1. **`raw_event` keeps dest's stale kickoff/name, discards source's correction** — **FIXED**. Now a conditional upsert (`ON CONFLICT DO UPDATE ... WHERE <any field differs>`) mirroring `vb.storage.save_raw_capture()`'s own live "latest wins" semantics. (A self-review pass the same day caught that the WHERE clause initially omitted the `sport` column despite it being in the SET list — also fixed.)
2. **`event_match_review` discards source's approved/rejected if dest still has pending** — **FIXED**. A reviewed row now always wins over a pending one regardless of which side it's on; two reviewed rows keep dest's decision (conservative, flagged for manual inspection rather than guessed at); `first_seen_at` is now reconciled to the earlier of the two sides (a gap the first version of this fix missed, caught by the same self-review pass).
3. **An existing settlement is skipped; a corrected score or better source isn't propagated** — **NOT FIXED**. `merge_databases.py`'s settlement merge still only inserts if no row exists for the natural key; it does not compare or update an existing settlement against a source correction.
4. **When opportunity core fields match, source-only snapshots are skipped via `continue`** — **NOT FIXED**. Unchanged from the audit's description.
5. **Same-timestamp snapshot reconciliation keeps the lowest ID without comparing content** — **NOT FIXED**. `reconcile_snapshot_duplicates()` is unchanged.
6. **`(market_key, first_cross_at)` identity doesn't merge genuinely overlapping streams** — **NOT FIXED**.
7. **Merge connection doesn't enable `PRAGMA foreign_keys=ON`** — **FIXED**. Added to `merge()`'s connection setup.
8. **A repeated merge on a true ID collision can create another variant** — **NOT FIXED**. This is a known, documented limitation in the module's own docstring, unchanged.

The audit's own recommended fix for F-11 is to "first eliminate the multi-master SQLite/cache model — a single authoritative store shrinks the error surface significantly" — i.e., points 3–6 and 8 are argued by the audit itself to be properly solved by the same VPS migration that resolves F-07, not by further patching the merge script in place. Points 1, 2, and 7 were closed because they were the two cases causing active, demonstrable data loss (stale corrections winning, human review decisions being discarded) plus a cheap, safe integrity addition — not because the rest were judged unimportant.

### F-12 / F-13 — PARTIALLY FIXED / FIXED (infrastructure), same caveat
`vb/fair_probability.py` implements power and odds-ratio de-vig methods (favorite-longshot-bias-aware, unlike Method B's plain proportional scaling) plus calibration metrics (log loss, Brier score, bucketed calibration, source dispersion) — this is real, tested infrastructure that directly answers both findings at the *methodology* level.

**What's not done**: the actual Method A definition used throughout this project — and the one the Phase 7 confirmatory experiment was frozen against, per the project owner's explicit choice — is still `vb/edge.py::raw_edge()`: the benchmark's own published odds treated as fair, no de-vig at all. The newer fair-probability methods exist and are usable (e.g. by `vb/feature_dataset.py`) but are not what "Method A" means operationally, and are not the primary fair model in the frozen protocol (`PROJECT_DOCUMENTATION.md` §0.4). This was a deliberate scope choice, confirmed with the project owner: the confirmatory experiment runs against the strategy already accumulating live shadow data, not a new one.

### F-14 — FIXED
`vb/market_mapping.py::match_events_v2` — a real maximum-weight bipartite assignment (`scipy.optimize.linear_sum_assignment`), replacing greedy nearest-first matching. `MatchOrientation` is now tracked explicitly, and a detected home/away swap is actually remapped (selection + Asian Handicap line sign flipped), not just logged as a warning string. Wired into `run_cycle_v2` via `find_leg_edges_v2`.

### F-15 — FIXED
`bet_decision`/`bet_execution` tables (`vb/models.py`) plus `vb/execution.py` (idempotent decision recording, latency-aware odds re-verification with a conservative-slippage convention, always-writes-one-row execution recording) implement the schema the audit itself proposed.

### F-16 — FIXED
Fixed 2026-07-29, then broadened the same day by self-review. `scripts/scheduled_run.py::main()` now returns whether the cycle's capture totally failed and whether the core `run_pipeline`/`run_pipeline_v2` pass crashed entirely; `__main__` exits non-zero on either condition (still 0 on partial capture degradation — some sites failing is expected/tolerable). `capture.yml`'s DB-backup and dashboard-deploy steps gained `if: always()` so a red exit code doesn't also silently skip the backup on exactly the day it matters most (a real regression the self-review pass caught in the first version of this fix, same day).

### F-17 — FIXED
`vb/settlement_evidence.py` — every settlement now traces to a `ResultEvidence` row and a versioned `SettlementVersion` row (insert-only; a correction is a new version pointing at the one it supersedes, never a silent overwrite). As of 2026-07-29, ESPN's raw scoreboard response is hashed (`vb/sources/results.py::find_result_with_evidence`) and attached to the evidence record — not a full byte archive (that's deferred, see below), but real tamper-evidence for what was actually recorded. Byte-level archiving of the three live odds-scraper HTTP paths remains deferred, since deciding where the archived bytes get durably stored is properly a Phase 2 (VPS) decision, and Phase 2 is declined for now.

### F-18 — FIXED
`vb/evaluation_v2.py` uses `flat_stake_profit` exclusively for its headline/primary metrics; the Phase 7 frozen protocol's own `primary_metric` field states this explicitly, matching the audit's own §18.1 language verbatim ("flat stake separates signal quality from sizing; Kelly is a secondary simulation"). `vb/evaluation.py`'s Kelly scenario (the older, v1 module) is presented alongside flat, never in place of it.

### F-19 — FIXED
`vb/settlement.py`'s `settle_totals`/`settle_handicap`/`settle_match_winner` explicitly `raise ValueError` on an invalid selection for the given market type, and reject a non-quarter-aligned handicap line, rather than silently falling through to a default branch.

### F-20 — PARTIALLY FIXED
Data/evaluation provenance (the finding's other half, overlapping F-06) is real: every `evaluation_run` carries `code_sha`, `config_hash`, and `db_snapshot_hash`. Fixed 2026-07-29: `requirements.txt` was unpinned (bare package names); now exact-pinned to the versions actually in use.

**What's not done**: there is still no `pyproject.toml` or hash-locked lockfile — `pip install -r requirements.txt` resolves transitive dependencies freely. This is *version*-reproducible, not *hash*-reproducible; a determined adversary or a compromised upstream package could still change what actually gets installed for a pinned version number. Not fixed further because it wasn't flagged as a priority relative to everything else in this pass — worth a follow-up if strict reproducibility matters more than it currently does for a paper-trading POC.

### F-21 — NOT FIXED
Confirmed directly against `vb/reporting.py`: `pre_entry_history`/`pre_entry_history_for_opportunity` still re-derive the comparison event via fuzzy matching (`_find_matching_event_id`) from raw snapshots at *report-generation* time, not from a stored, decision-time snapshot-id link — and the output isn't labeled `estimated_replay` or similar. This still feeds `scripts/build_dashboard.py` directly. Scoped entirely to the v1 legacy dashboard path (same reasoning as F-03's v1 half): not receiving further investment while v1 stays frozen as the legacy dataset's producer.

---

## What is explicitly not being pursued right now

Per the project owner's direct instruction (2026-07-29): **Betfair integration and the VPS migration are not priorities right now and are not being pursued further at this time.**

- **Betfair** (`vb/sources/betfair.py`): a complete, tested client exists — real JSON-RPC calls, session-expiry retry, commission-adjusted net-odds math — but is not wired into the live capture pipeline. Two blockers, neither of which the assistant can resolve unilaterally: a real Betfair account/API key (account creation is outside scope regardless of authorization), and a design decision on whether Betfair acts as a second benchmark or a comparison book (a real edge-detection semantics change, not a config toggle).
- **VPS migration** (Phase 2): fully researched (`VPS_MIGRATION_PLAN.md`), explicitly declined by the project owner after reviewing the cost/benefit tradeoff. This is the reason F-07 and 5 of F-11's 8 sub-cases remain open — the audit's own recommended fix for both is the same underlying architecture change.

Both are documented as deliberate decisions (`PROJECT_DOCUMENTATION.md` §0.5), not as gaps that were missed or deprioritized without discussion.

---

## Verification methodology used throughout this pass

Every fix listed as FIXED or PARTIALLY FIXED above was: (1) covered by a new or existing automated test — full suite is 334 tests, green as of this writing; (2) for anything touching the live capture/pipeline path, verified against a real `gh workflow run capture.yml` execution, watched to completion, with logs grepped for errors — not just "tests pass locally"; (3) where the fix touched the production database directly (the Phase 7 protocol freeze, the schema addition for it), verified against a real export/modify/import cycle against the live production data, including a follow-up export confirming the change survived a full restore-from-cache/capture/backup cycle intact. A proactive, high-effort self-review (8 finder angles, independently verified) was run against this entire remediation pass's own diff before considering it complete, and found and fixed 8 real issues in the fixes themselves (`PROJECT_DOCUMENTATION.md` §0.6) — including two that would have undermined earlier fixes in this same pass (F-16's exit code missing `if: always()` on the backup step; F-11's `raw_event` fix itself missing the `sport` column from its own conflict check).
