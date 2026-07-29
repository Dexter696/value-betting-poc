"""Merge one SQLite DB's data into another - built for reconciling the
local and GitHub-side vb.sqlite databases, which have run as two
independent capture streams and diverged.

The core row-by-row merge (raw_event/raw_market_snapshot/
event_match_review/settlement, and the opportunity/opportunity_snapshot
insert-or-rename logic below) is additive: it only ever inserts,
never deletes or overwrites an existing row's data. BUT `merge()` as a
whole is NOT purely additive - see the 2026-07-25 bug note further
down: it unconditionally runs reconcile_opportunity_groups()/
reconcile_snapshot_duplicates() at the end, and those DO delete rows
(including pre-existing duplicate groups in dest that predate this
particular merge call, not just rows this merge just inserted). Do not
rely on merge() as a whole being side-effect-free / safe to call
speculatively without a backup - it is a deliberate, documented
exception to the "additive only" rule below, not an oversight, but it
is real deletion.

Dedupes on each table's real natural key rather than raw autoincrement
ids, which have no meaning across two independently-created databases:
  - raw_event: (site, event_id) - source's row wins on a real conflict
    (matches vb.storage.save_raw_capture()'s own live ON CONFLICT DO
    UPDATE semantics - "latest capture wins", not "whichever database
    happened to be dest wins" - see the F-11 note below).
  - raw_market_snapshot: no declared natural key, so the full tuple
    (site, event_id, market_type, line, captured_at) is used - only
    genuinely new capture cycles get inserted.
  - event_match_review: (benchmark_site, benchmark_event_id,
    comparison_site, comparison_event_id) - a reviewed row (approved/
    rejected) always wins over a pending one from either side, so a
    real human decision can never be silently discarded just because
    of which database happened to be passed as dest (see the F-11 note
    below); settlement: its own schema UNIQUE constraints.

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

BUG FOUND 2026-07-25 (via a real duplicate report, ~41 duplicate rows /
35 groups in live data - the fix above wasn't enough): the collision
check above compares the full core-field tuple, which includes
`resolved_at`/`resolution_reason` - but those fields legitimately
change over an opportunity's own lifetime (null while open, then set
once it closes). Two capture streams that each independently tracked
the SAME real opportunity, one observing it before it closed and one
after (or via a different resolution_reason, e.g. one stream's cadence
caught `event_started` first while the other's caught
`dropped_below_threshold` a cycle earlier), produce two rows with
DIFFERING core tuples despite being the same real continuous
above-threshold period - so the old logic renamed-and-reinserted them
as if genuinely new, multiplying one real bet into two-plus rows in
every downstream report and dashboard tile.

The actual stable cross-database identity for an opportunity is
`(market_key, first_cross_at)`, not `instance_id` - `first_cross_at` is
set exactly once, at creation, and never changes, so two rows sharing
it are PROVABLY the same real period no matter what their
`resolved_at`/`resolution_reason`/instance_id happen to be.
`reconcile_opportunity_groups()` below enforces "at most one row per
(market_key, first_cross_at)" as a final invariant, unconditionally, at
the end of every merge - collapsing any group down to one surviving
instance (keeping the most objectively-complete state: an
`event_started` resolution beats any other, since kickoff time is a
deterministic wall-clock fact rather than something that depends on
which reading a given stream happened to sample; otherwise more
snapshots/most information wins) and unioning every group member's
snapshots into the survivor (deduped by captured_at) so no captured
data is lost in the collapse, only the artificial extra instance_id.
This runs as a safety net regardless of how a duplicate group
originated, so it's also the right tool to clean up already-corrupted
data directly (see scripts/reconcile_opportunities.py).

Usage: python scripts/merge_databases.py <source_db> <dest_db>
Merges source_db's rows INTO dest_db in place.
"""

import sys
from collections import defaultdict
from pathlib import Path

_RESOLUTION_RANK = {"event_started": 3, "market_suspended": 2, "dropped_below_threshold": 1, None: 0}


def reconcile_snapshot_duplicates(conn) -> dict:
    """Collapse opportunity_snapshot rows down to at most one per
    (opportunity_instance_id, captured_at). Found 2026-07-25 alongside
    the opportunity-level duplication bug above, in the SAME instance -
    a real, live pattern (32 groups / 42 excess rows in the production
    database at the time this was written), but its exact origin is NOT
    fully pinned down: vb/opportunity.py's ingest() timestamp-dedup guard
    (vb/opportunity.py:209-227) has been unchanged since the initial
    commit, ruling out "an older, buggier guard" as the cause. Most
    likely explanation is a residual of merge_databases.py's pre-6318535
    behavior (before that commit, EVERY source opportunity row was
    unconditionally inserted - see that commit's diff - which could
    plausibly have copied overlapping snapshot data across a rename).
    Root cause aside, keeping duplicate captured_at rows serves no
    purpose (the extra copies are near-identical - same edge_a/odds,
    only edge_b drifts by float noise - not meaningfully different
    readings), so this is fixed the same way as the opportunity-level
    case: run unconditionally as a final invariant, keep the
    lowest-id (earliest-inserted) row per group, drop the rest.
    """
    dup_groups = conn.execute(
        """
        SELECT opportunity_instance_id, captured_at, COUNT(*) as n
        FROM opportunity_snapshot
        GROUP BY opportunity_instance_id, captured_at
        HAVING n > 1
        """
    ).fetchall()

    removed = 0
    for instance_id, captured_at, _n in dup_groups:
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM opportunity_snapshot WHERE opportunity_instance_id = ? AND captured_at = ? ORDER BY id",
                (instance_id, captured_at),
            )
        ]
        for extra_id in ids[1:]:
            conn.execute("DELETE FROM opportunity_snapshot WHERE id = ?", (extra_id,))
            removed += 1

    conn.commit()
    return {"duplicate_snapshot_groups_collapsed": len(dup_groups), "duplicate_snapshots_removed": removed}


def reconcile_opportunity_groups(conn) -> dict:
    """Collapse opportunity rows down to at most one per (market_key,
    first_cross_at) - see the module docstring's 2026-07-25 bug note for
    why that pair, not instance_id, is the real identity. Idempotent and
    safe to run on any database regardless of how a duplicate group
    originated (a bad merge, manual data entry, anything) - a database
    with no duplicate groups is left untouched.
    """
    rows = conn.execute(
        "SELECT instance_id, market_key, first_cross_at, resolved_at, resolution_reason FROM opportunity"
    ).fetchall()

    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for instance_id, market_key, first_cross_at, resolved_at, resolution_reason in rows:
        groups[(market_key, first_cross_at)].append((instance_id, resolved_at, resolution_reason))

    def snapshot_count(instance_id: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM opportunity_snapshot WHERE opportunity_instance_id = ?", (instance_id,)
        ).fetchone()[0]

    groups_merged = instances_deleted = snapshots_moved = 0

    for (market_key, first_cross_at), entries in groups.items():
        if len(entries) <= 1:
            continue

        # Rank by: an event_started resolution (an objective wall-clock
        # fact, not sampling-dependent) beats any other reason; then more
        # snapshots (more complete observation); then lexicographically
        # smallest instance_id as a final deterministic tiebreak.
        ranked = sorted(
            entries,
            key=lambda e: (-_RESOLUTION_RANK[e[2]], -snapshot_count(e[0]), e[0]),
        )
        survivor_id = ranked[0][0]
        losers = ranked[1:]

        existing_captured = {
            row[0]
            for row in conn.execute(
                "SELECT captured_at FROM opportunity_snapshot WHERE opportunity_instance_id = ?", (survivor_id,)
            )
        }
        for loser_id, _loser_resolved_at, _loser_reason in losers:
            for srow in conn.execute(
                """
                SELECT captured_at, edge_a, edge_b, benchmark_odds, comparison_odds,
                       movement_source, max_bet_size, full_market_json
                FROM opportunity_snapshot WHERE opportunity_instance_id = ?
                """,
                (loser_id,),
            ).fetchall():
                if srow[0] in existing_captured:
                    continue  # same real reading, already present via the survivor or an earlier loser
                conn.execute(
                    """
                    INSERT INTO opportunity_snapshot
                        (opportunity_instance_id, captured_at, edge_a, edge_b, benchmark_odds,
                         comparison_odds, movement_source, max_bet_size, full_market_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (survivor_id, *srow),
                )
                existing_captured.add(srow[0])
                snapshots_moved += 1
            conn.execute("DELETE FROM opportunity_snapshot WHERE opportunity_instance_id = ?", (loser_id,))
            conn.execute("DELETE FROM opportunity WHERE instance_id = ?", (loser_id,))
            instances_deleted += 1

        groups_merged += 1

    conn.commit()
    return {
        "duplicate_groups_merged": groups_merged,
        "duplicate_instances_removed": instances_deleted,
        "snapshots_recovered_from_removed_instances": snapshots_moved,
    }


def merge(source_path: str, dest_path: str) -> dict:
    import sqlite3

    dest = sqlite3.connect(dest_path)
    dest.execute("PRAGMA foreign_keys = ON")
    dest.execute("ATTACH DATABASE ? AS src", (str(source_path),))
    counts: dict[str, int] = {}

    # NOT a plain INSERT OR IGNORE (F-11, 2026-07-29): that keeps dest's
    # row unconditionally on conflict, but vb.storage.save_raw_capture()'s
    # own live upsert path (ON CONFLICT DO UPDATE) always takes the
    # LATEST capture's data - a merge that instead always kept the older
    # side would silently reintroduce whichever of the two streams
    # captured a stale/wrong value first (e.g. the 2026-07-29 Swisslos
    # timezone bug corrupted kickoff_utc for weeks; a merge in the wrong
    # direction could have kept the corrupted value even after the fix
    # landed and the other stream recaptured the correct one). Mirrors
    # the live path exactly: source's row always wins on conflict.
    # SQLite's grammar rejects INSERT...SELECT...ON CONFLICT DO UPDATE as
    # a single bulk statement ("near DO: syntax error") - the upsert
    # clause is only accepted after a VALUES source, so this is a
    # per-row loop rather than one bulk statement.
    raw_event_touched = 0
    for row in dest.execute("""
        SELECT site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team FROM src.raw_event
    """).fetchall():
        cur = dest.execute("""
            INSERT INTO main.raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site, event_id) DO UPDATE SET
                sport = excluded.sport, competition = excluded.competition, kickoff_utc = excluded.kickoff_utc,
                raw_home_team = excluded.raw_home_team, raw_away_team = excluded.raw_away_team
            WHERE main.raw_event.sport != excluded.sport OR main.raw_event.competition != excluded.competition
               OR main.raw_event.kickoff_utc != excluded.kickoff_utc OR main.raw_event.raw_home_team != excluded.raw_home_team
               OR main.raw_event.raw_away_team != excluded.raw_away_team
        """, row)
        raw_event_touched += cur.rowcount
    counts["raw_event"] = raw_event_touched

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

    # NOT a plain INSERT OR IGNORE (F-11, 2026-07-29): that always keeps
    # dest's row on conflict, which is directionally unsafe for a human
    # review decision - if dest's row is still 'pending' but source's
    # copy of the SAME candidate was already reviewed (status
    # approved/rejected, reviewed_at set), a naive merge would silently
    # discard that real human decision depending only on which database
    # happened to be passed as dest. Policy: a reviewed row always wins
    # over a pending one regardless of side; two reviewed rows keep
    # dest's decision (conservative - a genuine double-review conflict
    # is rare enough to leave the SUM(*) unchanged for later manual
    # inspection rather than guess); two pending rows just take the
    # later last_seen_at as a plain "still open, seen again" update.
    event_match_review_touched = 0
    for row in dest.execute("""
        SELECT benchmark_site, benchmark_event_id, comparison_site, comparison_event_id,
               score, reasons_json, first_seen_at, last_seen_at, status, reviewed_at
        FROM src.event_match_review
    """).fetchall():
        (bench_site, bench_id, comp_site, comp_id, score, reasons_json, first_seen_at, last_seen_at, status, reviewed_at) = row
        existing = dest.execute("""
            SELECT status FROM main.event_match_review
            WHERE benchmark_site = ? AND benchmark_event_id = ? AND comparison_site = ? AND comparison_event_id = ?
        """, (bench_site, bench_id, comp_site, comp_id)).fetchone()

        if existing is None:
            dest.execute("""
                INSERT INTO main.event_match_review
                    (benchmark_site, benchmark_event_id, comparison_site, comparison_event_id, score, reasons_json, first_seen_at, last_seen_at, status, reviewed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, row)
            event_match_review_touched += 1
            continue

        (dest_status,) = existing
        if dest_status == "pending" and status != "pending":
            # source has a real decision dest lacks - take it, along with
            # whichever last_seen_at is actually later (freshest sighting)
            # and whichever first_seen_at is actually earlier (true
            # earliest sighting - omitted from the first version of this
            # fix, found by self-review: same lossy-merge failure mode
            # F-11 exists to close, just on a different column).
            dest.execute("""
                UPDATE main.event_match_review
                SET score = ?, reasons_json = ?, first_seen_at = MIN(first_seen_at, ?),
                    last_seen_at = MAX(last_seen_at, ?), status = ?, reviewed_at = ?
                WHERE benchmark_site = ? AND benchmark_event_id = ? AND comparison_site = ? AND comparison_event_id = ?
            """, (score, reasons_json, first_seen_at, last_seen_at, status, reviewed_at, bench_site, bench_id, comp_site, comp_id))
            event_match_review_touched += 1
        elif dest_status != "pending":
            pass  # dest already has a decision (own or a prior merge's) - never overwritten by source
        else:
            # both still pending - reconcile first_seen_at (earliest wins)
            # and last_seen_at (latest wins) independently; only counted
            # as touched if either side's value actually changes dest's.
            cur = dest.execute("""
                UPDATE main.event_match_review
                SET first_seen_at = MIN(first_seen_at, ?), last_seen_at = MAX(last_seen_at, ?)
                WHERE benchmark_site = ? AND benchmark_event_id = ? AND comparison_site = ? AND comparison_event_id = ?
                  AND (first_seen_at > ? OR last_seen_at < ?)
            """, (first_seen_at, last_seen_at, bench_site, bench_id, comp_site, comp_id, first_seen_at, last_seen_at))
            event_match_review_touched += cur.rowcount
    counts["event_match_review"] = event_match_review_touched

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

    # Safety net (see the 2026-07-25 bug note above): run unconditionally,
    # not just when this merge's own collision logic renamed something -
    # a duplicate group could already exist in dest from a prior run, or
    # from any other source entirely. Snapshot-level cleanup runs second,
    # after opportunity-level collapsing has settled which instance ids
    # actually survive.
    counts.update(reconcile_opportunity_groups(dest))
    counts.update(reconcile_snapshot_duplicates(dest))

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
