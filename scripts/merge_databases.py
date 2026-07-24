"""Merge one SQLite DB's data into another, additively (never deletes or
overwrites an existing row's data) - built for reconciling the local and
GitHub-side vb.sqlite databases, which have run as two independent
capture streams and diverged.

Dedupes on each table's real natural key rather than raw autoincrement
ids, which have no meaning across two independently-created databases:
  - raw_event: (site, event_id) - static match info, dest's row wins on
    conflict (shouldn't meaningfully differ between sources).
  - raw_market_snapshot: no declared natural key, so the full tuple
    (site, event_id, market_type, line, captured_at) is used - only
    genuinely new capture cycles get inserted.
  - event_match_review / settlement: their own schema UNIQUE constraints.

opportunity.instance_id ("{market_key}#{seq}") is a different problem:
it's deterministic *within* one database, not guaranteed unique *across*
two independently-run ones that happened to track the same market_key.
On a collision, the incoming opportunity (and its opportunity_snapshot
rows) are renumbered to continue the destination's own sequence for that
market_key, rather than the naive-merge outcomes of silently dropping
one side's data (INSERT OR IGNORE) or silently overwriting it
(INSERT OR REPLACE) - both lose real tracked history, which is exactly
what this script exists to avoid.

This is meant for a one-time (or infrequent, deliberate) reconciliation,
not a repeatable sync: re-running with the same source against an
already-merged dest can reintroduce duplicates for any opportunity that
collided and was renumbered on a prior run (the renumbered copy no
longer matches the original instance_id a second pass would look for).
Run it once per source snapshot.

Usage: python scripts/merge_databases.py <source_db> <dest_db>
Merges source_db's rows INTO dest_db in place.
"""

import sys
from pathlib import Path


def merge(source_path: str, dest_path: str) -> dict:
    import sqlite3

    dest = sqlite3.connect(dest_path)
    dest.execute("ATTACH DATABASE ? AS src", (str(source_path),))
    counts: dict[str, int] = {}

    cur = dest.execute("INSERT OR IGNORE INTO main.raw_event SELECT * FROM src.raw_event")
    counts["raw_event"] = cur.rowcount

    cur = dest.execute("""
        INSERT INTO main.raw_market_snapshot (site, event_id, market_type, line, outcomes_json, max_bet_size, captured_at)
        SELECT s.site, s.event_id, s.market_type, s.line, s.outcomes_json, s.max_bet_size, s.captured_at
        FROM src.raw_market_snapshot s
        WHERE NOT EXISTS (
            SELECT 1 FROM main.raw_market_snapshot d
            WHERE d.site = s.site AND d.event_id = s.event_id AND d.market_type = s.market_type
              AND (d.line IS s.line OR d.line = s.line) AND d.captured_at = s.captured_at
        )
    """)
    counts["raw_market_snapshot"] = cur.rowcount

    cur = dest.execute("""
        INSERT OR IGNORE INTO main.event_match_review
            (benchmark_site, benchmark_event_id, comparison_site, comparison_event_id, score, reasons_json, first_seen_at, last_seen_at, status, reviewed_at)
        SELECT benchmark_site, benchmark_event_id, comparison_site, comparison_event_id, score, reasons_json, first_seen_at, last_seen_at, status, reviewed_at
        FROM src.event_match_review
    """)
    counts["event_match_review"] = cur.rowcount

    cur = dest.execute("""
        INSERT OR IGNORE INTO main.settlement
            (benchmark_site, benchmark_event_id, market_type, line, selection, outcome, home_goals, away_goals, settled_at, source)
        SELECT benchmark_site, benchmark_event_id, market_type, line, selection, outcome, home_goals, away_goals, settled_at, source
        FROM src.settlement
    """)
    counts["settlement"] = cur.rowcount

    dest_ids = {row[0] for row in dest.execute("SELECT instance_id FROM main.opportunity")}
    next_seq: dict[str, int] = {}
    for instance_id, market_key in dest.execute("SELECT instance_id, market_key FROM main.opportunity"):
        seq = int(instance_id.rsplit("#", 1)[-1])
        next_seq[market_key] = max(next_seq.get(market_key, -1), seq)

    opp_rows = dest.execute("""
        SELECT instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
               line, selection, first_cross_at, resolved_at, resolution_reason
        FROM src.opportunity
    """).fetchall()

    inserted_opps = inserted_snaps = renamed = 0
    for (instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
         line, selection, first_cross_at, resolved_at, resolution_reason) in opp_rows:

        final_id = instance_id
        if final_id in dest_ids:
            next_seq[market_key] = next_seq.get(market_key, -1) + 1
            final_id = f"{market_key}#{next_seq[market_key]}"
            while final_id in dest_ids:
                next_seq[market_key] += 1
                final_id = f"{market_key}#{next_seq[market_key]}"
            renamed += 1
        dest_ids.add(final_id)

        dest.execute("""
            INSERT INTO main.opportunity
                (instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
                 line, selection, first_cross_at, resolved_at, resolution_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (final_id, market_key, sport, benchmark_site, comparison_site, market_type,
              line, selection, first_cross_at, resolved_at, resolution_reason))
        inserted_opps += 1

        snap_rows = dest.execute("""
            SELECT captured_at, edge_a, edge_b, benchmark_odds, comparison_odds, movement_source, max_bet_size, full_market_json
            FROM src.opportunity_snapshot WHERE opportunity_instance_id = ?
            ORDER BY id
        """, (instance_id,)).fetchall()
        for srow in snap_rows:
            dest.execute("""
                INSERT INTO main.opportunity_snapshot
                    (opportunity_instance_id, captured_at, edge_a, edge_b, benchmark_odds, comparison_odds, movement_source, max_bet_size, full_market_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (final_id, *srow))
            inserted_snaps += 1

    counts["opportunity"] = inserted_opps
    counts["opportunity_snapshot"] = inserted_snaps
    counts["opportunity_renamed_on_collision"] = renamed

    dest.commit()
    dest.execute("DETACH DATABASE src")
    dest.close()
    return counts


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        return
    source_path, dest_path = sys.argv[1], sys.argv[2]
    if not Path(source_path).exists():
        print(f"source not found: {source_path}")
        return
    if not Path(dest_path).exists():
        print(f"dest not found: {dest_path}")
        return
    counts = merge(source_path, dest_path)
    for table, n in counts.items():
        print(f"{table}: +{n}")


if __name__ == "__main__":
    main()
