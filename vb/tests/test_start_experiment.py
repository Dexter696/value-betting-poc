import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import start_experiment  # noqa: E402

from vb.opportunity import THRESHOLD  # noqa: E402
from vb.protocol import require_active_protocol  # noqa: E402
from vb.storage import init_db  # noqa: E402


def test_main_freezes_a_real_protocol_for_method_a(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "vb.sqlite"
    monkeypatch.setattr(start_experiment, "DB_PATH", db_path)

    start_experiment.main()

    conn = init_db(db_path)
    protocol = require_active_protocol(conn, start_experiment.EXPERIMENT_NAME)
    assert protocol.name == "method-a-confirmatory"
    assert protocol.exposure_limits == {"max_stake_per_event": 3.0, "max_stake_per_site": 25.0}
    assert protocol.execution_haircut_s == 1.0
    assert "200" in protocol.end_rule
    assert len(protocol.strategy_version_ids) == 1
    assert set(protocol.source_list) == {"pinnacle.com", "swisslos.ch", "loro.ch"}


def test_main_resolves_the_same_strategy_version_the_shadow_pipeline_uses(tmp_path, monkeypatch):
    # StrategyDefinition is content-addressed - freezing this protocol
    # must resolve to the SAME row scripts/scheduled_run.py's
    # run_pipeline_v2 already uses for Method A, not a new one, so the
    # frozen protocol actually covers the decisions already flowing
    # through the live shadow pipeline.
    from datetime import datetime, timezone

    from vb.identity import content_hash, new_id
    from vb.models import StrategyDefinition
    from vb.storage import get_or_create_strategy_definition

    db_path = tmp_path / "vb.sqlite"
    monkeypatch.setattr(start_experiment, "DB_PATH", db_path)
    conn = init_db(db_path)

    live_config = {"signal_model": "raw-v1", "threshold": THRESHOLD, "shadow_mode": True}
    live_strategy_id = get_or_create_strategy_definition(conn, StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=THRESHOLD,
        max_age_s=start_experiment.FRESHNESS_MAX_AGE_S, max_skew_s=start_experiment.FRESHNESS_MAX_SKEW_S,
        min_lead_time_s=start_experiment.FRESHNESS_MIN_LEAD_TIME_S, config=live_config,
        config_hash=content_hash(live_config), created_at=datetime.now(timezone.utc),
    ))

    start_experiment.main()

    protocol = require_active_protocol(conn, start_experiment.EXPERIMENT_NAME)
    assert protocol.strategy_version_ids == (live_strategy_id,)


def test_main_refuses_to_silently_supersede_an_existing_protocol(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "vb.sqlite"
    monkeypatch.setattr(start_experiment, "DB_PATH", db_path)

    start_experiment.main()
    conn = init_db(db_path)
    first = require_active_protocol(conn, start_experiment.EXPERIMENT_NAME)

    start_experiment.main()  # accidental second run

    second = require_active_protocol(conn, start_experiment.EXPERIMENT_NAME)
    assert second.id == first.id  # unchanged - refused to supersede
