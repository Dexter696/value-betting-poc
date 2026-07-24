# VB methodology — addendum (opportunity lifecycle / edge evolution)

Added 2026-07-23. Supplements the "Data" and "Evaluation" sections of
`VB - methodology.docx`; not merged into the .docx because it appears to
be open in Word (a `~$` lock file is present) — fold this in by hand, or
ask to have it merged once the file is closed.

## Opportunity lifecycle

An opportunity is a continuous period, not a single snapshot.

1. **Definition.** An opportunity begins when a match+market+leg's edge
   first crosses the 3% threshold (Method A, raw) and ends when: the edge
   drops back below threshold, the market is suspended, or the event
   starts. One continuous period above threshold = one opportunity
   instance. A later re-crossing of the same leg is a **new instance**,
   linked to prior instances via a shared `market_key` so re-occurrence
   can be analyzed.

2. **Time-series capture.** For the entire life of an open opportunity,
   keep sampling at the standard cadence (>= 1/min) and store one snapshot
   row per sample: timestamp, edge under Method A and Method B, the full
   market for all 4 books (every outcome + each book's margin/overround),
   and max allowed bet at that moment. Sampling does not stop once the
   edge is already large enough to qualify as a bet — the whole trajectory
   is captured.

3. **Derived per-opportunity fields:** entry edge (first cross), peak edge
   and time-to-peak, the full ordered edge trajectory, and convergence
   time (first cross → drop below threshold). Convergence time is now one
   derived field of the lifecycle, not the primary capture unit.

4. **Movement attribution.** Each snapshot records whether the gap moved
   because the benchmark odds moved, the comparison odds moved, or both
   (vs. the immediately preceding snapshot for that same leg). A gap that
   widens because the benchmark moved (news/injury) may just be a stale
   comparison line; a gap that widens because the comparison drifted while
   the benchmark held is the real signal. Needed to tell these apart at
   evaluation time.

5. **Entry policy stays a post-processing choice** (same philosophy as
   Method A vs B): since the full trajectory is stored, evaluation can
   later simulate entry at first-cross, at peak, or at every snapshot, and
   compare. Capture never hard-codes a single entry point.

## Data-model implication

One-to-many: an `opportunity` header row linked to many
`opportunity_snapshot` rows. Implemented in `vb/opportunity.py` (lifecycle
dataclasses + `OpportunityTracker`) and `vb/schema.sql` /
`vb/storage.py` (SQLite persistence). See those files for the concrete
shape.
