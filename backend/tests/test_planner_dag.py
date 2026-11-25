"""Tests for Plan DAG validation, topological sorting, cycles, and stacked PR branches."""

from __future__ import annotations

import pytest

from nightshift.engine.planner import (
    PhaseDefinition,
    get_runnable_phases,
    resolve_base_branch,
    validate_plan_dag,
)


def test_plan_dag_topological_sort() -> None:
    # Phase 1 -> Phase 2 -> Phase 3, plus Phase 4 independent
    p1 = PhaseDefinition(id="p1", title="Foundation", branch="feat/p1")
    p2 = PhaseDefinition(id="p2", title="Engine", branch="feat/p2", depends_on=["p1"])
    p3 = PhaseDefinition(id="p3", title="UI", branch="feat/p3", depends_on=["p2"])
    p4 = PhaseDefinition(id="p4", title="Docs", branch="feat/p4")

    # Pass in shuffled order
    shuffled = [p3, p4, p2, p1]
    sorted_phases = validate_plan_dag(shuffled)
    order_ids = [p.id for p in sorted_phases]

    assert order_ids.index("p1") < order_ids.index("p2")
    assert order_ids.index("p2") < order_ids.index("p3")


def test_plan_dag_cycle_detection() -> None:
    # Cycle: p1 -> p2 -> p3 -> p1
    p1 = PhaseDefinition(id="p1", title="P1", branch="feat/p1", depends_on=["p3"])
    p2 = PhaseDefinition(id="p2", title="P2", branch="feat/p2", depends_on=["p1"])
    p3 = PhaseDefinition(id="p3", title="P3", branch="feat/p3", depends_on=["p2"])

    with pytest.raises(ValueError, match="Cyclic dependency detected"):
        validate_plan_dag([p1, p2, p3])


def test_plan_dag_missing_dependency() -> None:
    p1 = PhaseDefinition(id="p1", title="P1", branch="feat/p1", depends_on=["non_existent"])
    with pytest.raises(ValueError, match="depends on non-existent phase"):
        validate_plan_dag([p1])


def test_plan_rejects_missing_branch_and_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="needs a branch"):
        validate_plan_dag([PhaseDefinition(id="p1", title="P1", branch="  ")])

    with pytest.raises(ValueError, match="unique ID"):
        validate_plan_dag([
            PhaseDefinition(id="p1", title="First", branch="feat/first"),
            PhaseDefinition(id="p1", title="Second", branch="feat/second"),
        ])


def test_runnable_phases_and_stacked_branches() -> None:
    p1 = PhaseDefinition(id="p1", title="P1", branch="feat/p1")
    p2 = PhaseDefinition(id="p2", title="P2", branch="feat/p2", depends_on=["p1"])
    p3 = PhaseDefinition(id="p3", title="P3", branch="feat/p3")  # independent
    all_phases = [p1, p2, p3]
    phases_by_id = {p.id: p for p in all_phases}

    # Initial state: p1 and p3 are runnable, p2 is waiting on p1
    runnable = get_runnable_phases(all_phases, completed_phase_ids=set())
    assert {p.id for p in runnable} == {"p1", "p3"}

    # Case: p1 is parked -> independent phase p3 continues!
    # And p2 is stacked on p1's branch per BLUEPRINT 5d!
    parked = {"p1"}
    base_p2 = resolve_base_branch(p2, phases_by_id, parked_ids=parked, default_base="main")
    assert base_p2 == "feat/p1"  # Stacked PR cut from parked branch!

    # p3 continues from main
    base_p3 = resolve_base_branch(p3, phases_by_id, parked_ids=parked, default_base="main")
    assert base_p3 == "main"
