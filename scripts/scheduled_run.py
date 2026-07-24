"""Full capture+pipeline cycle, meant to be run on a schedule (e.g. via
Windows Task Scheduler). Captures all three working sites, then runs the
matching+edge pipeline for each comparison site vs Pinnacle.

Logs to data/logs/scheduler.log (and stdout). Each site's capture is
isolated in its own try/except so one site failing (a scraper breaking,
a network hiccup) doesn't take down the whole cycle or block the others.
"""

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
from vb.sources.swisslos import SwisslosClient
from vb.storage import init_db, prune_raw_snapshots, save_raw_capture

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


def capture_loro(conn) -> None:
    client = LoroClient(headless=True)
    rows = client.fetch_football()
    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)
    log.info("loro: captured %d events, %d snapshots", len(rows), sum(len(r.snapshots) for r in rows))


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
    log.info("=== cycle start ===")
    conn = init_db(DB_PATH)

    for name, fn in [("pinnacle", capture_pinnacle), ("swisslos", capture_swisslos), ("loro", capture_loro)]:
        try:
            fn(conn)
        except Exception:
            log.error("%s capture failed:\n%s", name, traceback.format_exc())

    try:
        run_pipeline(conn)
    except Exception:
        log.error("pipeline run failed:\n%s", traceback.format_exc())

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
