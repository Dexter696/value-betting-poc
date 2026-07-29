"""Immutable, pre-registered experiment protocol - Phase 7 of the
2026-07-25 external audit's remediation roadmap.

The audit's requirement (§17, "Fáze 7 - Nový předregistrovaný paper
experiment"): before the first decision of a confirmatory (non-shadow)
paper-trading run, save an immutable protocol naming the experiment id,
start/end rule, active strategy versions, source list, freshness/skew/
lead-time, fair model, entry threshold and persistence, execution
latency/haircut, exposure and stake, primary and secondary metrics, the
sample/stopping rule, and the incident/version-change policy - so none
of those can drift in response to how early results look. A change
after the experiment starts must end the old strategy cohort and begin
a new one; it must never retroactively recompute old decisions as if
the new rule had always applied.

This module only builds the RECORDING mechanism, not one particular
experiment's actual VALUES. `strategy_version` already covers threshold/
persistence/freshness/skew/lead-time per cohort (StrategyDefinition is
itself immutable and content-addressed, F-06) - `strategy_version_ids`
here is the set of those active under this protocol. Everything else
(start/end rule, source list, fair model, execution haircut, exposure/
stake limits, and above all the sample/stopping rule) is a business or
statistical decision only the project owner can make; freeze_protocol()
is what a future scripts/start_experiment.py would call once those
values are actually decided, not something this module invents on its
own. See PROJECT_DOCUMENTATION.md's Phase 7 status for why no real
experiment has been frozen yet.

The v2 shadow pipeline running on every capture cycle right now
(scripts/scheduled_run.py) is deliberately NOT gated behind an
ExperimentProtocol - it is an accumulation/shadow phase, not the
confirmatory run this module exists for. require_active_protocol() is
the guard a real experiment's decision path would call before
recording any decision, once a protocol actually exists to check
against.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .identity import new_id
from .models import ExperimentProtocol
from .storage import current_experiment_protocol, mark_experiment_protocol_superseded, save_experiment_protocol


class ProtocolError(Exception):
    pass


def freeze_protocol(
    conn,
    name: str,
    start_rule: str,
    end_rule: str,
    strategy_version_ids: Sequence[str],
    source_list: Sequence[str],
    fair_model: str,
    execution_haircut_s: float,
    exposure_limits: dict,
    primary_metric: str,
    secondary_metrics: Sequence[str],
    incident_policy: str,
    now: datetime,
) -> str:
    """Records a new immutable protocol for `name`. If a protocol
    already exists for this name, the new freeze supersedes it
    (`superseded_by`, never an edit/delete) - a real, deliberate new
    cohort starts, and everything already decided/tracked under the
    previous protocol's strategy versions stays exactly as it was
    evaluated at the time.

    Raises ProtocolError rather than silently accepting an
    underspecified protocol - a missing strategy_version_ids list would
    mean "cohort with no rules", which defeats the entire point of
    pre-registration.
    """
    if not strategy_version_ids:
        raise ProtocolError("a protocol must name at least one active strategy_version")
    if not source_list:
        raise ProtocolError("a protocol must name at least one source")

    previous = current_experiment_protocol(conn, name)
    protocol_id = new_id()
    save_experiment_protocol(conn, ExperimentProtocol(
        id=protocol_id, name=name, frozen_at=now, start_rule=start_rule, end_rule=end_rule,
        strategy_version_ids=tuple(strategy_version_ids), source_list=tuple(source_list),
        fair_model=fair_model, execution_haircut_s=execution_haircut_s, exposure_limits=exposure_limits,
        primary_metric=primary_metric, secondary_metrics=tuple(secondary_metrics), incident_policy=incident_policy,
    ))
    if previous is not None:
        mark_experiment_protocol_superseded(conn, previous.id, protocol_id)
    return protocol_id


def require_active_protocol(conn, name: str) -> ExperimentProtocol:
    """Guard for a real (non-shadow) decision path: raises ProtocolError
    if no protocol has been frozen yet for `name`, rather than silently
    letting a decision get recorded with no pre-registered rules behind
    it."""
    protocol = current_experiment_protocol(conn, name)
    if protocol is None:
        raise ProtocolError(f"no experiment_protocol frozen for {name!r} - call freeze_protocol() before recording decisions")
    return protocol
