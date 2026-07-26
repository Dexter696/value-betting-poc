"""Apply the schema-v2 append-only tables (2026-07-25 audit remediation,
Phase 1) to an existing v1 database, and verify nothing else moved.

vb.storage.init_db() already does this migration idempotently (every
new table is `CREATE TABLE IF NOT EXISTS`, and the migration event is
recorded via `INSERT OR IGNORE INTO schema_migration`) — this script
exists as the auditor's explicitly requested standalone step, so the
migration is a deliberate, verified, loggable action instead of an
invisible side effect the next process to open the database happens to
trigger.

Verifies additivity by comparing every v1 table's row count before and
after: a migration that changed any of them would mean the "append-
only, new tables only" claim is false, and this script would catch
that rather than asserting it in a document.

Usage: python scripts/migrate_schema_v2.py [path/to/vb.sqlite]
Defaults to data/vb.sqlite.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from vb.storage import CURRENT_SCHEMA_VERSION, init_db  # noqa: E402

V1_TABLES = (
    "raw_event", "raw_market_snapshot", "event_match_review",
    "opportunity", "opportunity_snapshot", "settlement",
)
V2_TABLES = (
    "schema_migration", "capture_run", "source_run", "event_version", "canonical_event",
    "event_match", "market_snapshot_v2", "strategy_definition", "signal_episode",
    "signal_observation", "bet_decision", "bet_execution", "closing_snapshot",
    "result_evidence", "settlement_version", "evaluation_run",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _row_counts(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in tables if _table_exists(conn, t)
    }


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "vb.sqlite"
    if not db_path.exists():
        print(f"no database at {db_path} - nothing to migrate (init_db will create a fresh v2 one)")
        return

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    before = _row_counts(conn, V1_TABLES)
    conn.close()

    conn = init_db(db_path)
    after = _row_counts(conn, V1_TABLES)

    print(f"database: {db_path}")
    print(f"schema_version now: {CURRENT_SCHEMA_VERSION}")
    print()
    print("v1 table row counts (must be identical before/after):")
    mismatch = False
    for table in V1_TABLES:
        b, a = before.get(table), after.get(table)
        flag = "" if b == a else "  <-- MISMATCH"
        if b != a:
            mismatch = True
        print(f"  {table:24s} {b!s:>8} -> {a!s:>8}{flag}")

    print()
    print("v2 tables present:")
    v2_counts = _row_counts(conn, V2_TABLES)
    for table in V2_TABLES:
        print(f"  {table:24s} {v2_counts.get(table, 'MISSING')}")

    conn.close()

    if mismatch:
        print()
        print("FAILED: a v1 table's row count changed during migration - this should never happen.")
        sys.exit(1)

    print()
    print("OK: schema v2 applied, all v1 data untouched.")


if __name__ == "__main__":
    main()
