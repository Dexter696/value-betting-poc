from datetime import datetime, timedelta, timezone

import pytest

from vb.protocol import ProtocolError, freeze_protocol, require_active_protocol
from vb.storage import current_experiment_protocol, get_experiment_protocol, init_db

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _freeze(conn, name="method-a-confirmatory", now=T0, strategy_version_ids=("sv-1",)):
    return freeze_protocol(
        conn, name=name, start_rule="first eligible signal after freeze", end_rule="200 clustered canonical events or 90 days",
        strategy_version_ids=strategy_version_ids, source_list=("pinnacle.com", "swisslos.ch", "loro.ch"),
        fair_model="power", execution_haircut_s=5.0, exposure_limits={"max_stake_per_event": 3.0, "max_stake_per_site": 25.0},
        primary_metric="flat net ROI, clustered by canonical event", secondary_metrics=("CLV_fair", "acceptance_rate"),
        incident_policy="a code/rule change ends the current cohort and starts a new protocol version", now=now,
    )


def test_freeze_protocol_persists_and_is_readable_back(tmp_path):
    conn = _db(tmp_path)
    protocol_id = _freeze(conn)

    protocol = get_experiment_protocol(conn, protocol_id)
    assert protocol is not None
    assert protocol.name == "method-a-confirmatory"
    assert protocol.strategy_version_ids == ("sv-1",)
    assert protocol.source_list == ("pinnacle.com", "swisslos.ch", "loro.ch")
    assert protocol.exposure_limits == {"max_stake_per_event": 3.0, "max_stake_per_site": 25.0}
    assert protocol.secondary_metrics == ("CLV_fair", "acceptance_rate")
    assert protocol.superseded_by is None


def test_freeze_protocol_rejects_empty_strategy_version_ids(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ProtocolError):
        _freeze(conn, strategy_version_ids=())


def test_current_experiment_protocol_returns_none_before_any_freeze(tmp_path):
    conn = _db(tmp_path)
    assert current_experiment_protocol(conn, "method-a-confirmatory") is None


def test_require_active_protocol_raises_when_none_frozen(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ProtocolError):
        require_active_protocol(conn, "method-a-confirmatory")


def test_require_active_protocol_returns_it_once_frozen(tmp_path):
    conn = _db(tmp_path)
    protocol_id = _freeze(conn)
    assert require_active_protocol(conn, "method-a-confirmatory").id == protocol_id


def test_a_second_freeze_supersedes_the_first_without_editing_it(tmp_path):
    conn = _db(tmp_path)
    first_id = _freeze(conn, now=T0, strategy_version_ids=("sv-1",))
    second_id = _freeze(conn, now=T0 + timedelta(days=30), strategy_version_ids=("sv-2",))

    first = get_experiment_protocol(conn, first_id)
    assert first.superseded_by == second_id
    assert first.strategy_version_ids == ("sv-1",)  # old row itself never edited

    active = current_experiment_protocol(conn, "method-a-confirmatory")
    assert active.id == second_id
    assert active.strategy_version_ids == ("sv-2",)


def test_two_different_experiment_names_dont_supersede_each_other(tmp_path):
    conn = _db(tmp_path)
    a_id = _freeze(conn, name="method-a-confirmatory")
    b_id = _freeze(conn, name="method-b-confirmatory")

    assert current_experiment_protocol(conn, "method-a-confirmatory").id == a_id
    assert current_experiment_protocol(conn, "method-b-confirmatory").id == b_id
