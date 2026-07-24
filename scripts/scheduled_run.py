"""Full capture+pipeline cycle, meant to be run on a schedule (e.g. via
Windows Task Scheduler). Captures all three working sites, then runs the
matching+edge pipeline for each comparison site vs Pinnacle.

Logs to data/logs/scheduler.log (and stdout). Each site's capture is
isolated in its own try/except so one site failing (a scraper breaking,
a network hiccup) doesn't take down the whole cycle or block the others.
"""

import argparse
import logging
import sys
import traceback
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

from vb.pipeline import run_cycle
from vb.sources.loro import LoroClient
from vb.sources.pinnacle import PinnacleClient
from vb.sources.results import find_result
from vb.sources.swisslos import SwisslosClient
from vb.storage import init_db, list_unsettled_matches, prune_raw_snapshots, record_match_result, save_raw_capture

DB_PATH = ROOT / "data" / "vb.sqlite"
RAW_SNAPSHOT_RETENTION_HOURS = 24


def capture_pinnacle(conn) -> None:
    client = PinnacleClient()
    rows = client.fetch_all_soccer()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info(
        "pinnacle: captured %d events, %d snapshots (all soccer leagues)",
        len(rows), sum(len(r.snapshots) for r in rows),
    )


def capture_swisslos(conn) -> None:
    client = SwisslosClient(headless=True)
    rows = client.fetch_football()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info("swisslos: captured %d events, %d snapshots", len(rows), sum(len(r.snapshots) for r in rows))


def capture_swisslos_full(conn) -> None:
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


def capture_swisslos_handicaps(conn) -> None:
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

    handicap_rows = client.fetch_all_handicaps(rows)
    for row in handicap_rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info(
        "swisslos (asian handicap): captured handicap markets for %d/%d matches, %d snapshot(s)",
        len(handicap_rows), len(rows), sum(len(r.snapshots) for r in handicap_rows),
    )


def capture_loro(conn) -> None:
    client = LoroClient(headless=True)
    rows = client.fetch_football()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info("loro: captured %d events, %d snapshots", len(rows), sum(len(r.snapshots) for r in rows))


def auto_settle(conn) -> None:
    # ESPN doesn't cover every league Pinnacle offers (see
    # vb.sources.results docstring) - matches it can't place still show
    # up in list_unsettled_matches next cycle and eventually need
    # scripts/record_result.py by hand. That's expected, not an error.
    unsettled = list_unsettled_matches(conn, "pinnacle.com")
    settled_count = 0
    for event in unsettled:
        result = find_result(event.competition, event.raw_home_team, event.raw_away_team, event.kickoff_utc)
        if result is None:
            continue
        home_goals, away_goals = result
        record_match_result(conn, "pinnacle.com", event.event_id, home_goals, away_goals, source="auto:espn")
        settled_count += 1
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


def main() -> None:
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

    if args.full_handicaps:
        steps = [("pinnacle", capture_pinnacle), ("swisslos handicaps", capture_swisslos_handicaps)]
    else:
        swisslos_fn = capture_swisslos_full if args.full_swisslos else capture_swisslos
        steps = [("pinnacle", capture_pinnacle), ("swisslos", swisslos_fn), ("loro", capture_loro)]

    for name, fn in steps:
        try:
            fn(conn)
        except Exception:
            log.error("%s capture failed:\n%s", name, traceback.format_exc())

    try:
        run_pipeline(conn)
    except Exception:
        log.error("pipeline run failed:\n%s", traceback.format_exc())

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


if __name__ == "__main__":
    main()
