"""One-off cleanup for a real bug found in merge_databases.py (fixed in
the same commit as this script): the opportunity-collision-renaming
logic didn't check whether an incoming "colliding" row was actually
identical data (same underlying opportunity, re-appearing because two
DBs shared ancestry from an earlier sync) before renaming and
re-inserting it as if it were genuinely new - and the settlement merge
used a raw INSERT OR IGNORE, which SQLite's NULL-never-equals-NULL
UNIQUE-constraint semantics don't dedupe on (every match_winner
settlement has line IS NULL, so every one of them got treated as
non-conflicting and duplicated). Both bugs together turned one merge
into ~140 duplicate opportunity rows and 16 duplicate settlement rows.

This removes exact duplicates only (identical core fields), never
touches non-duplicate data. Safe to run multiple times.

Usage: python scripts/dedupe_after_bad_merge.py <db_path>
"""

import sqlite3
import sys


def dedupe_settlement(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id, benchmark_site, benchmark_event_id, market_type, line, selection FROM settlement ORDER BY id").fetchall()
    seen = set()
    to_delete = []
    for row_id, site, event_id, market_type, line, selection in rows:
        key = (site, event_id, market_type, line, selection)
        if key in seen:
            to_delete.append(row_id)
        else:
            seen.add(key)
    for row_id in to_delete:
        conn.execute("DELETE FROM settlement WHERE id = ?", (row_id,))
    return len(to_delete)


def dedupe_opportunities(conn: sqlite3.Connection) -> tuple[int, int]:
    rows = conn.execute("""
        SELECT instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
               line, selection, first_cross_at, resolved_at, resolution_reason
        FROM opportunity ORDER BY instance_id
    """).fetchall()

    seen = set()
    dup_instance_ids = []
    for (instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
         line, selection, first_cross_at, resolved_at, resolution_reason) in rows:
        core = (market_key, sport, benchmark_site, comparison_site, market_type,
                line, selection, first_cross_at, resolved_at, resolution_reason)
        if core in seen:
            dup_instance_ids.append(instance_id)
        else:
            seen.add(core)

    snap_count = 0
    for iid in dup_instance_ids:
        cur = conn.execute("DELETE FROM opportunity_snapshot WHERE opportunity_instance_id = ?", (iid,))
        snap_count += cur.rowcount
        conn.execute("DELETE FROM opportunity WHERE instance_id = ?", (iid,))

    return len(dup_instance_ids), snap_count


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        return
    conn = sqlite3.connect(sys.argv[1])
    settlement_removed = dedupe_settlement(conn)
    opp_removed, snap_removed = dedupe_opportunities(conn)
    conn.commit()
    print(f"settlement duplicates removed: {settlement_removed}")
    print(f"opportunity duplicates removed: {opp_removed}")
    print(f"orphaned opportunity_snapshot rows removed: {snap_removed}")


if __name__ == "__main__":
    main()
