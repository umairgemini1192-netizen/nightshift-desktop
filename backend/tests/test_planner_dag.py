"""Tests for Plan DAG validation, topological sorting, cycles, and stacked PR branches."""

from __future__ import annotations

import pytest

from nightshift.engine.planner import (
    PhaseDefinition,
    export_queue_json,
    get_runnable_phases,
    import_queue_json,
    resolve_base_branch,
    validate_plan,
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


def test_validate_plan_plain_english_problems() -> None:
    # 1. Missing branch
    p_no_branch = PhaseDefinition(id="p1", title="No Branch", branch="   ")
    probs = validate_plan([p_no_branch])
    assert any("needs a branch" in p for p in probs)

    # 2. Cycle in depends_on
    p1 = PhaseDefinition(id="p1", title="P1", branch="feat/p1", depends_on=["p2"])
    p2 = PhaseDefinition(id="p2", title="P2", branch="feat/p2", depends_on=["p1"])
    probs = validate_plan([p1, p2])
    assert any("Cyclic dependency detected" in p for p in probs)

    # 3. Unknown ladder
    p_bad_ladder = PhaseDefinition(id="p3", title="Bad Ladder", branch="feat/p3", ladder="quantum-solver")
    probs = validate_plan([p_bad_ladder], known_ladders={"backend", "ui", "default"})
    assert any("specifies unknown ladder 'quantum-solver'" in p for p in probs)

    # 4. Unknown dependency
    p_bad_dep = PhaseDefinition(id="p4", title="Bad Dep", branch="feat/p4", depends_on=["ghost"])
    probs = validate_plan([p_bad_dep])
    assert any("depends on non-existent phase 'ghost'" in p for p in probs)

    # 5. Valid plan
    p_valid_1 = PhaseDefinition(id="p1", title="Valid 1", branch="feat/p1", ladder="backend")
    p_valid_2 = PhaseDefinition(id="p2", title="Valid 2", branch="feat/p2", depends_on=["p1"], ladder="ui")
    assert validate_plan([p_valid_1, p_valid_2]) == []


def test_queue_json_import_export_round_trip() -> None:
    legacy_payload = {
        "repo": "owner/drivelift-android",
        "local_repo": "D:/DevCache/drivelift",
        "desktop_apk_dir": "D:/DevCache/drivelift/release",
        "auto_merge": True,
        "auto_release": False,
        "max_ci_retries": 3,
        "quota_cooldown_minutes": 45,
        "runner_timeout_minutes": 120,
        "failover_ladders": {
            "backend": ["agy", "gemini", "codex", "claude"],
            "ui": ["agy", "gemini", "claude"],
        },
        "phases": [
            {
                "id": "p1",
                "title": "Phase 1: Database Migration",
                "branch": "feat/p1-db",
                "prompt": "Apply schema migrations cleanly",
                "depends_on": [],
                "ladder": "backend",
                "timeout_minutes": 60,
                "auto_merge": True,
                "release_after_merge": False,
                "force_rerun": False,
            },
            {
                "id": "p2",
                "title": "Phase 2: UI Dashboard",
                "branch": "feat/p2-ui",
                "prompt": "Build Compose dashboard screen",
                "depends_on": ["p1"],
                "ladder": "ui",
                "timeout_minutes": 90,
                "auto_merge": True,
                "release_after_merge": False,
                "force_rerun": False,
            },
        ],
    }

    # Import
    imported = import_queue_json(legacy_payload)
    assert imported["project"]["repo"] == "owner/drivelift-android"
    assert imported["project"]["local_path"] == "D:/DevCache/drivelift"
    assert len(imported["phases"]) == 2
    assert imported["settings"]["max_ci_retries"] == 3

    # Export
    exported = export_queue_json(
        project=imported["project"],
        plan=imported["plan"],
        phases=imported["phases"],
        ladders=imported["ladders"],
        settings=imported["settings"],
    )

    assert exported["repo"] == legacy_payload["repo"]
    assert exported["local_repo"] == legacy_payload["local_repo"]
    assert exported["desktop_apk_dir"] == legacy_payload["desktop_apk_dir"]
    assert exported["auto_merge"] == legacy_payload["auto_merge"]
    assert exported["auto_release"] == legacy_payload["auto_release"]
    assert exported["max_ci_retries"] == legacy_payload["max_ci_retries"]
    assert exported["quota_cooldown_minutes"] == legacy_payload["quota_cooldown_minutes"]
    assert exported["runner_timeout_minutes"] == legacy_payload["runner_timeout_minutes"]
    assert exported["failover_ladders"] == legacy_payload["failover_ladders"]
    assert exported["phases"] == legacy_payload["phases"]

