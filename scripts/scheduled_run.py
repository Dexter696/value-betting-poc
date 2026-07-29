"""Full capture+pipeline cycle, meant to be run on a schedule (e.g. via
Windows Task Scheduler). Captures all three working sites, then runs the
matching+edge pipeline for each comparison site vs Pinnacle.

Logs to data/logs/scheduler.log (and stdout). Each site's capture is
isolated in its own try/except so one site failing (a scraper breaking,
a network hiccup) doesn't take down the whole cycle or block the others.

Also runs the schema-v2 pipeline (vb.capture_v2/vb.pipeline.run_cycle_v2)
as a SHADOW alongside the v1 capture/pipeline above: every v1 step is
completely unchanged and remains the sole source of truth for the
current dashboard/experiment, and every v2 step is wrapped in its own
try/except that only logs on failure - a bug in the still-new v2 path
must never be able to look like v1 itself failed, or to block v1 from
completing. This is the deliberate first real-data cutover step noted
in vb/capture_v2.py's and vb/pipeline.py's module docstrings: v2 starts
accumulating genuine capture_run/source_run/signal_episode history
under real (if currently irregular - see the audit's F-07 finding)
production cadence, which is what Phase 4 step 4 (a time-aligned
feature dataset) and Phase 6/7 (real evaluation, a real experiment)
both need before they can do anything with actual data rather than
synthetic test fixtures. It does not affect what the live dashboard
shows or what counts as the current experiment's result - that's still
entirely v1, per PROJECT_DOCUMENTATION.md's Phase 0/1 notices.
"""

import argparse
import logging
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scheduler.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vb.scheduler")

from vb.capture_v2 import end_capture_run, record_source_capture, record_source_failure, start_capture_run
from vb.closing import process_cycle_closings
from vb.decision_runner import process_cycle_decisions
from vb.exposure import ExposureLimits
from vb.freshness import FreshnessLimits
from vb.identity import content_hash, new_id
from vb.models import RunStatus, StrategyDefinition
from vb.opportunity import THRESHOLD
from vb.pipeline import run_cycle, run_cycle_v2
from vb.settlement_evidence import record_settlement_for_event
from vb.strategy import ImmediateEntryPolicy
from vb.sources.loro import LoroClient
from vb.sources.pinnacle import PinnacleClient
from vb.sources.results import find_result_with_evidence
from vb.sources.swisslos import SwisslosClient
from vb.storage import (
    CURRENT_SCHEMA_VERSION,
    force_resolve_stale_opportunities,
    get_or_create_strategy_definition,
    init_db,
    list_unsettled_matches,
    prune_raw_snapshots,
    record_match_result,
    save_raw_capture,
)

DB_PATH = ROOT / "data" / "vb.sqlite"
RAW_SNAPSHOT_RETENTION_HOURS = 24

# Shadow-mode freshness limits (Phase 1's F-01 gate), deliberately loose
# to match the CURRENT real (irregular - see the audit's F-07 finding:
# median ~36min gap between scheduled runs, not the every-minute cadence
# Phase 2's VPS migration is meant to provide) capture cadence. These are
# NOT the tightened limits a real pre-registered experiment (Phase 7)
# would use - they exist so shadow-mode v2 observations are eligible
# often enough to be useful for exercising the new pipeline against real
# data, while still being a real, honest gate rather than disabled.
SHADOW_FRESHNESS_LIMITS = FreshnessLimits(max_age_s=3 * 3600, max_skew_s=1800, min_lead_time_s=1800)

# Shadow-mode exposure ceiling (Phase 5 step 6): every shadow bet is a
# flat 1.0-unit stake, so these caps just bound how many concurrent
# legs on the same real event / the same comparison site the shadow
# pipeline will let itself "hold" at once - a real, honest limit
# (not disabled), sized generously for a paper-trading shadow rather
# than the tighter risk limits a real pre-registered experiment
# (Phase 7) would set.
SHADOW_EXPOSURE_LIMITS = ExposureLimits(max_stake_per_event=3.0, max_stake_per_site=25.0)


def _git_sha() -> str:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _shadow_capture_v2(conn, capture_run_id: str, site: str, mode: str, captures: list, v2_ok: list) -> None:
    """Best-effort v2 provenance recording alongside an ALREADY-
    successful v1 capture (this only runs after v1's own save_raw_capture
    loop has completed without raising) - never allowed to make the
    calling capture_* function look like it failed; a bug here is a v2
    problem, not a "the scraper broke" problem."""
    try:
        record_source_capture(conn, capture_run_id, site, mode, captures)
        v2_ok.append(True)
    except Exception:
        log.error("v2 shadow capture failed for %s (%s):\n%s", site, mode, traceback.format_exc())
        v2_ok.append(False)


def capture_pinnacle(conn, capture_run_id: str, v2_ok: list) -> None:
    client = PinnacleClient()
    rows = client.fetch_all_soccer()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info(
        "pinnacle: captured %d events, %d snapshots (all soccer leagues)",
        len(rows), sum(len(r.snapshots) for r in rows),
    )
    _shadow_capture_v2(conn, capture_run_id, "pinnacle.com", "quick", [(r.event, r.snapshots) for r in rows], v2_ok)


def capture_swisslos(conn, capture_run_id: str, v2_ok: list) -> None:
    client = SwisslosClient(headless=True)
    rows = client.fetch_football()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info("swisslos: captured %d events, %d snapshots", len(rows), sum(len(r.snapshots) for r in rows))
    _shadow_capture_v2(conn, capture_run_id, "swisslos.ch", "quick", [(r.event, r.snapshots) for r in rows], v2_ok)


def capture_swisslos_full(conn, capture_run_id: str, v2_ok: list) -> None:
    # Full country-by-country sweep (~290s) - takes much longer than the
    # quick single-page fetch_football(), which is why this runs on its
    # own, less-frequent schedule rather than every cycle. See
    # SwisslosClient.fetch_all_countries() docstring for why a concurrent
    # version wasn't kept (measured slower on this machine).
    client = SwisslosClient(headless=True)
    rows = client.fetch_all_countries()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info("swisslos (full breadth): captured %d events, %d snapshots", len(rows), sum(len(r.snapshots) for r in rows))
    _shadow_capture_v2(conn, capture_run_id, "swisslos.ch", "full", [(r.event, r.snapshots) for r in rows], v2_ok)


def capture_swisslos_handicaps(conn, capture_run_id: str, v2_ok: list) -> None:
    # Full Asian Handicap sweep: re-fetches full country breadth first
    # (to get a fresh detail_url per match - the "t=" match id isn't
    # persisted anywhere in storage, see SwisslosClient module docstring)
    # then visits every match's own detail page for its Asian Handicap
    # market. At ~7s/page across hundreds of matches, this is by far the
    # heaviest single step in this script (tens of minutes) - runs on its
    # own, much slower schedule (see capture.yml), never every cycle.
    client = SwisslosClient(headless=True)
    rows = client.fetch_all_countries()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info(
        "swisslos (full breadth, for handicap sweep): captured %d events, %d snapshots",
        len(rows), sum(len(r.snapshots) for r in rows),
    )
    _shadow_capture_v2(conn, capture_run_id, "swisslos.ch", "full", [(r.event, r.snapshots) for r in rows], v2_ok)

    handicap_rows = client.fetch_all_handicaps(rows)
    for row in handicap_rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info(
        "swisslos (asian handicap): captured handicap markets for %d/%d matches, %d snapshot(s)",
        len(handicap_rows), len(rows), sum(len(r.snapshots) for r in handicap_rows),
    )
    _shadow_capture_v2(
        conn, capture_run_id, "swisslos.ch", "handicap", [(r.event, r.snapshots) for r in handicap_rows], v2_ok
    )


def capture_loro(conn, capture_run_id: str, v2_ok: list) -> None:
    client = LoroClient(headless=True)
    rows = client.fetch_football()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info("loro: captured %d events, %d snapshots", len(rows), sum(len(r.snapshots) for r in rows))
    _shadow_capture_v2(conn, capture_run_id, "loro.ch", "quick", [(r.event, r.snapshots) for r in rows], v2_ok)


def force_resolve_stale(conn) -> None:
    # Safety net for opportunities that never got the normal live close
    # (see vb.storage.force_resolve_stale_opportunities docstring for why
    # that can happen) - runs every cycle so nothing stays incorrectly
    # "open" for more than one cycle past the buffer, same spirit as
    # auto_settle running every cycle rather than as a rare manual step.
    count = force_resolve_stale_opportunities(conn)
    if count:
        log.info("force-resolve: closed %d stale open opportunity instance(s)", count)


def auto_settle(conn) -> None:
    # ESPN doesn't cover every league Pinnacle offers (see
    # vb.sources.results docstring) - matches it can't place still show
    # up in list_unsettled_matches next cycle and eventually need
    # scripts/record_result.py by hand. That's expected, not an error.
    unsettled = list_unsettled_matches(conn, "pinnacle.com")
    settled_count = 0
    for event in unsettled:
        result = find_result_with_evidence(event.competition, event.raw_home_team, event.raw_away_team, event.kickoff_utc)
        if result is None:
            continue
        home_goals, away_goals = result.score
        record_match_result(conn, "pinnacle.com", event.event_id, home_goals, away_goals, source="auto:espn")
        settled_count += 1

        # F-17/Phase 6: also record versioned settlement evidence for
        # every v2 leg ever tracked on this event - best-effort, wrapped
        # so a v2-side bug can never make v1's own (already-succeeded)
        # settlement look like it failed.
        try:
            now = datetime.now(timezone.utc)
            legs = record_settlement_for_event(
                conn, "pinnacle.com", event.event_id, provider="espn",
                home_goals=home_goals, away_goals=away_goals, now=now,
                raw_payload_hash=result.raw_payload_hash, source_url=result.source_url,
            )
            if legs:
                log.info("settlement evidence v2: %d leg(s) settled for event %s", legs, event.event_id)
        except Exception:
            log.error("settlement evidence v2 failed for event %s:\n%s", event.event_id, traceback.format_exc())
    log.info("auto-settle: %d/%d unsettled match(es) resolved via ESPN", settled_count, len(unsettled))


def run_pipeline(conn) -> None:
    for comparison_site in ("swisslos.ch", "loro.ch"):
        touched = run_cycle(conn, "pinnacle.com", comparison_site)
        open_count = sum(1 for o in touched if o.is_open)
        closed_count = len(touched) - open_count
        log.info(
            "pipeline vs %s: %d instance(s) touched (%d open, %d closed)",
            comparison_site, len(touched), open_count, closed_count,
        )


def run_pipeline_v2(conn) -> None:
    """Shadow-mode schema-v2 pipeline pass (see this module's own
    docstring) - Method A only for now (raw edge, matching v1's real
    production threshold), reading whatever v2 capture_v2 has recorded
    this cycle. A Method-B shadow strategy is a trivial follow-up
    (same call, a devigged-v1 StrategyDefinition and
    `edge_selector=lambda leg: leg.edge_b`) once there's a reason to
    want it running continuously rather than just tested."""
    now = datetime.now(timezone.utc)
    config = {"signal_model": "raw-v1", "threshold": THRESHOLD, "shadow_mode": True}
    strategy = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=THRESHOLD,
        max_age_s=SHADOW_FRESHNESS_LIMITS.max_age_s, max_skew_s=SHADOW_FRESHNESS_LIMITS.max_skew_s,
        min_lead_time_s=SHADOW_FRESHNESS_LIMITS.min_lead_time_s, config=config,
        config_hash=content_hash(config), created_at=now,
    )
    get_or_create_strategy_definition(conn, strategy)

    entry_policy = ImmediateEntryPolicy(threshold=THRESHOLD)
    for comparison_site in ("swisslos.ch", "loro.ch"):
        results = run_cycle_v2(
            conn, "pinnacle.com", comparison_site, strategy, SHADOW_FRESHNESS_LIMITS,
            edge_selector=lambda leg: leg.edge_a,
        )
        opened = sum(1 for r in results if r.opened)
        closed = sum(1 for r in results if r.closed)
        ineligible = sum(1 for r in results if r.episode_id is None and not r.opened)
        log.info(
            "pipeline v2 (shadow) vs %s: %d reading(s) processed (%d opened, %d closed, %d ineligible/below-threshold)",
            comparison_site, len(results), opened, closed, ineligible,
        )

        executions = process_cycle_decisions(conn, results, entry_policy, limits=SHADOW_EXPOSURE_LIMITS)
        if executions:
            log.info(
                "decisions v2 (shadow) vs %s: %d new bet_decision/bet_execution recorded",
                comparison_site, len(executions),
            )

        closings = process_cycle_closings(conn, results, now=now)
        if closings:
            log.info("closing consensus v2 (shadow) vs %s: %d snapshot(s) recorded", comparison_site, closings)


def main() -> RunStatus:
    """Returns the cycle's capture_run status. F-16: every stage below is
    deliberately isolated in its own try/except so one failing site or
    pipeline step never aborts the rest of the cycle - but that same
    isolation previously meant NOTHING ever propagated failure back to
    the process exit code, so a cycle where every single capture site
    failed still looked green in GitHub Actions. The caller (__main__)
    now exits non-zero on a FAILED capture_run - a total capture
    failure is a real signal worth a red CI check, unlike PARTIAL
    (some sites failed, expected/tolerable degradation)."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full-swisslos", action="store_true",
        help="Use the full country-by-country Swisslos sweep instead of the quick single-page fetch. "
             "Takes ~5min longer - meant for a less-frequent schedule (~15-20min), not every cycle.",
    )
    parser.add_argument(
        "--full-handicaps", action="store_true",
        help="Sweep every Swisslos match's own detail page for its Asian Handicap market, on top of "
             "the full country breadth. Tens of minutes - meant for a much slower schedule (daily) than "
             "--full-swisslos, and replaces the normal Swisslos/Loro captures for this cycle rather than "
             "stacking on top of them.",
    )
    args = parser.parse_args()

    log.info("=== cycle start (full_swisslos=%s, full_handicaps=%s) ===", args.full_swisslos, args.full_handicaps)
    conn = init_db(DB_PATH)

    capture_run_id = start_capture_run(conn, git_sha=_git_sha(), schema_version=CURRENT_SCHEMA_VERSION)
    v2_ok: list = []

    if args.full_handicaps:
        steps = [
            ("pinnacle", capture_pinnacle, "pinnacle.com", "quick"),
            ("swisslos handicaps", capture_swisslos_handicaps, "swisslos.ch", "handicap"),
        ]
    else:
        swisslos_fn = capture_swisslos_full if args.full_swisslos else capture_swisslos
        swisslos_mode = "full" if args.full_swisslos else "quick"
        steps = [
            ("pinnacle", capture_pinnacle, "pinnacle.com", "quick"),
            ("swisslos", swisslos_fn, "swisslos.ch", swisslos_mode),
            ("loro", capture_loro, "loro.ch", "quick"),
        ]

    for name, fn, site, mode in steps:
        try:
            fn(conn, capture_run_id, v2_ok)
        except Exception:
            log.error("%s capture failed:\n%s", name, traceback.format_exc())
            # F-16: a failed v1 fetch still gets a terminal, honest v2
            # source_run row - "no status at all" is exactly what let a
            # partial-failure cycle look like it never ran.
            try:
                record_source_failure(conn, capture_run_id, site, mode, error_code="fetch_failed", error_summary=str(sys.exc_info()[1]))
            except Exception:
                log.error("v2 shadow failure-recording itself failed for %s:\n%s", site, traceback.format_exc())
            v2_ok.append(False)

    capture_run_status = RunStatus.SUCCESS if all(v2_ok) else (RunStatus.PARTIAL if any(v2_ok) else RunStatus.FAILED)
    try:
        end_capture_run(conn, capture_run_id, capture_run_status)
    except Exception:
        log.error("v2 end_capture_run failed:\n%s", traceback.format_exc())

    try:
        run_pipeline(conn)
    except Exception:
        log.error("pipeline run failed:\n%s", traceback.format_exc())

    try:
        run_pipeline_v2(conn)
    except Exception:
        log.error("pipeline v2 (shadow) run failed:\n%s", traceback.format_exc())

    try:
        force_resolve_stale(conn)
    except Exception:
        log.error("force-resolve failed:\n%s", traceback.format_exc())

    try:
        auto_settle(conn)
    except Exception:
        log.error("auto-settle failed:\n%s", traceback.format_exc())

    try:
        deleted = prune_raw_snapshots(conn, keep_hours=RAW_SNAPSHOT_RETENTION_HOURS)
        if deleted:
            conn.execute("VACUUM")
            conn.commit()
        log.info("pruned %d old raw snapshot row(s) (retention: %dh)", deleted, RAW_SNAPSHOT_RETENTION_HOURS)
    except Exception:
        log.error("pruning failed:\n%s", traceback.format_exc())

    log.info("=== cycle end ===")
    return capture_run_status


if __name__ == "__main__":
    status = main()
    if status == RunStatus.FAILED:
        sys.exit(1)
