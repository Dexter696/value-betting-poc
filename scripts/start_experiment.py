"""Freeze the real, pre-registered Phase 7 experiment protocol
(audit 2026-07-25, §17) for Method A - a one-time, deliberate action,
not something scheduled_run.py calls automatically.

Values below were confirmed with the project owner on 2026-07-29, not
invented here:
  - Method A only (raw-v1, >=3% edge) - what's already running
    continuously in shadow mode; Method B would need new wiring first.
  - Stopping rule: 200 clustered canonical events.
  - Exposure limits: reuse the existing shadow-mode values.

Method A's strategy_version has already been accumulating shadow
decisions for days before this freeze (infrastructure testing, not
results-driven parameter tuning - the threshold/limits below are the
same values that have been in place since Phase 5, unchanged). Per
Phase 7's own integrity requirement ("before the first decision..."),
those earlier decisions must NOT retroactively count as confirmatory
data - start_rule below states the real boundary explicitly, and
vb.evaluation_runner.run_evaluation's `since=` parameter is how a
report actually enforces it.

Usage: python scripts/start_experiment.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.identity import content_hash, new_id
from vb.models import StrategyDefinition
from vb.opportunity import THRESHOLD
from vb.protocol import freeze_protocol
from vb.storage import current_experiment_protocol, get_or_create_strategy_definition, init_db

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
EXPERIMENT_NAME = "method-a-confirmatory"

# Must match scripts/scheduled_run.py's SHADOW_FRESHNESS_LIMITS/
# SHADOW_EXPOSURE_LIMITS exactly, so the frozen protocol's
# strategy_version resolves to the SAME row already running live
# (StrategyDefinition is content-addressed - get_or_create_strategy_
# definition returns the existing id for an identical config rather
# than minting a new one).
FRESHNESS_MAX_AGE_S = 3 * 3600
FRESHNESS_MAX_SKEW_S = 1800
FRESHNESS_MIN_LEAD_TIME_S = 1800
EXECUTION_HAIRCUT_S = 1.0  # matches vb.decision_runner.SHADOW_LATENCY
EXPOSURE_LIMITS = {"max_stake_per_event": 3.0, "max_stake_per_site": 25.0}

PRIMARY_METRIC = (
    "flat net ROI from verified/accepted paper executions, clustered by canonical event, "
    "after slippage and all known costs (audit doc S18.1)"
)
SECONDARY_METRICS = (
    "mean_CLV_fair", "median_CLV_fair", "clv_95pct_event_cluster_interval",
    "quote_verification_rate", "acceptance_fill_rate", "odds_slippage",
    "freshness_skew_reject_rate", "profit_by_site_market_odds_bucket",
    "max_drawdown", "event_exposure",
)


def main() -> None:
    conn = init_db(DB_PATH)
    now = datetime.now(timezone.utc)

    existing = current_experiment_protocol(conn, EXPERIMENT_NAME)
    if existing is not None:
        print(f"An active protocol already exists for {EXPERIMENT_NAME!r} (id={existing.id}, frozen_at={existing.frozen_at}).")
        print("Refusing to silently supersede it - re-run vb.protocol.freeze_protocol() directly if a real rule change is intended.")
        return

    config = {"signal_model": "raw-v1", "threshold": THRESHOLD, "shadow_mode": True}
    strategy = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=THRESHOLD,
        max_age_s=FRESHNESS_MAX_AGE_S, max_skew_s=FRESHNESS_MAX_SKEW_S,
        min_lead_time_s=FRESHNESS_MIN_LEAD_TIME_S, config=config,
        config_hash=content_hash(config), created_at=now,
    )
    strategy_version = get_or_create_strategy_definition(conn, strategy)

    protocol_id = freeze_protocol(
        conn, name=EXPERIMENT_NAME,
        start_rule=(
            "First bet_decision with decided_at >= frozen_at for this strategy_version. "
            "Earlier shadow-mode decisions under the same strategy_version (accumulated before this "
            "freeze, for infrastructure testing) are explicitly excluded - see vb.evaluation_runner."
            "run_evaluation(since=frozen_at)."
        ),
        end_rule="200 distinct settled canonical_event clusters under this strategy_version, or manual stop - whichever first",
        strategy_version_ids=(strategy_version,),
        source_list=("pinnacle.com", "swisslos.ch", "loro.ch"),
        fair_model="raw-v1: benchmark's own implied probability, no de-vig (vb.edge.raw_edge) - Method A as defined throughout this project",
        execution_haircut_s=EXECUTION_HAIRCUT_S,
        exposure_limits=EXPOSURE_LIMITS,
        primary_metric=PRIMARY_METRIC,
        secondary_metrics=SECONDARY_METRICS,
        incident_policy=(
            "Any code or rule change after this freeze ends this protocol version (superseded_by a new "
            "freeze_protocol() call under the same name) and starts a new cohort. Decisions/episodes "
            "already recorded under this protocol's strategy_version are never retroactively treated as "
            "belonging to a different rule set."
        ),
        now=now,
    )

    print(f"Frozen experiment_protocol: {protocol_id}")
    print(f"  name: {EXPERIMENT_NAME}")
    print(f"  strategy_version: {strategy_version}")
    print(f"  frozen_at: {now.isoformat()}")
    print(f"  end_rule: 200 distinct settled canonical_event clusters, or manual stop")


if __name__ == "__main__":
    main()
