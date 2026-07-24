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
A same-instance_id row is only treated as a genuine collision (and
renumbered, so both sides' data survive) if its core fields actually
differ; if they're byte-identical it's skipped outright rather than
renamed-and-reinserted as if it were new data - two DBs that share
ancestry (e.g. both descend from an earlier sync) will have plenty of
already-identical instance_ids, and treating those as fresh collisions
duplicated ~140 rows the first time this ran for real. Naive
alternatives (INSERT OR IGNORE / INSERT OR REPLACE) were rejected up
front since both silently lose real tracked history on a *true*
collision, which is exactly what this script exists to avoid.

Re-running with the same source against an already-merged dest is safe
for the shared-ancestry case (identical rows are recognized and
skipped, not re-duplicated). It is NOT safe for a true collision that
was renumbered on a prior run: the renumbered copy has a different
instance_id than the original source row, so a second pass won't
recognize it as already-merged and will renumber-and-insert again.
Prefer running it once per source snapshot.

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

    # NOT a plain INSERT OR IGNORE: SQLite's UNIQUE constraint never
    # treats two NULLs as equal, and `line` is NULL for every
    # match_winner settlement (the majority) - a naive INSERT OR IGNORE
    # would treat every one of those as "new" and duplicate it even
    # though it's already present (this is the same NULL-vs-NULL gotcha
    # vb.storage.save_settlement() already works around; a raw merge
    # query needs the same NULL-safe existence check, found the hard way
    # after it silently duplicated 16 settlement rows on a real merge).
    settlement_inserted = 0
    for row in dest.execute("""
        SELECT benchmark_site, benchmark_event_id, market_type, line, selection, outcome, home_goals, away_goals, settled_at, source
        FROM src.settlement
    """).fetchall():
        site, event_id, market_type, line, selection = row[0], row[1], row[2], row[3], row[4]
        exists = dest.execute("""
            SELECT 1 FROM main.settlement
            WHERE benchmark_site = ? AND benchmark_event_id = ? AND market_type = ?
              AND (line IS ? OR line = ?) AND selection = ?
        """, (site, event_id, market_type, line, line, selection)).fetchone()
        if exists:
            continue
        dest.execute("""
            INSERT INTO main.settlement
                (benchmark_site, benchmark_event_id, market_type, line, selection, outcome, home_goals, away_goals, settled_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, row)
        settlement_inserted += 1
    counts["settlement"] = settlement_inserted

    # Keyed on the full core-field tuple, not just instance_id: two DBs
    # that share ancestry (e.g. both descend from an earlier merge/sync)
    # will have plenty of instance_ids that already match AND already
    # hold identical data - those must be skipped outright, not
    # renamed-and-reinserted as if they were new (found the hard way
    # after a same-ancestry merge produced ~140 duplicate opportunity
    # rows this way). Only a same-instance_id row with DIFFERENT core
    # data is a true collision worth renaming to preserve both sides.
    existing = {}  # instance_id -> core tuple
    dest_ids = set()
    next_seq: dict[str, int] = {}
    for (instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
         line, selection, first_cross_at, resolved_at, resolution_reason) in dest.execute(
        "SELECT instance_id, market_key, sport, benchmark_site, comparison_site, market_type, "
        "line, selection, first_cross_at, resolved_at, resolution_reason FROM main.opportunity"
    ):
        dest_ids.add(instance_id)
        existing[instance_id] = (market_key, sport, benchmark_site, comparison_site, market_type,
                                  line, selection, first_cross_at, resolved_at, resolution_reason)
        seq = int(instance_id.rsplit("#", 1)[-1])
        next_seq[market_key] = max(next_seq.get(market_key, -1), seq)

    opp_rows = dest.execute("""
        SELECT instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
               line, selection, first_cross_at, resolved_at, resolution_reason
        FROM src.opportunity
    """).fetchall()

    inserted_opps = inserted_snaps = renamed = skipped_identical = 0
    for (instance_id, market_key, sport, benchmark_site, comparison_site, market_type,
         line, selection, first_cross_at, resolved_at, resolution_reason) in opp_rows:

        core = (market_key, sport, benchmark_site, comparison_site, market_type,
                line, selection, first_cross_at, resolved_at, resolution_reason)

        if instance_id in dest_ids:
            if existing[instance_id] == core:
                skipped_identical += 1
                continue  # already present, byte-identical - not a real collision
            next_seq[market_key] = next_seq.get(market_key, -1) + 1
            final_id = f"{market_key}#{next_seq[market_key]}"
            while final_id in dest_ids:
                next_seq[market_key] += 1
                final_id = f"{market_key}#{next_seq[market_key]}"
            renamed += 1
        else:
            final_id = instance_id
        dest_ids.add(final_id)
        existing[final_id] = core

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
    counts["opportunity_skipped_identical"] = skipped_identical

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
