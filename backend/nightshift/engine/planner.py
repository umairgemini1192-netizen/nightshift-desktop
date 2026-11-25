"""Plan loader, DAG validator, and phase execution planner (BLUEPRINT 5, 5d)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nightshift.store import Store


@dataclass(frozen=True)
class PhaseDefinition:
    id: str
    title: str
    branch: str
    prompt: str = ""
    depends_on: list[str] = field(default_factory=list)
    ladder: str = "default"
    size: str = "medium"
    timeout_minutes: int = 180
    auto_merge: bool = True
    release_after_merge: bool = False
    force_rerun: bool = False
    position: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhaseDefinition:
        raw_deps = data.get("depends_on") or data.get("depends_on_json") or []
        if isinstance(raw_deps, str):
            try:
                deps = json.loads(raw_deps)
            except Exception:
                deps = []
        else:
            deps = list(raw_deps)

        return cls(
            id=str(data["id"]),
            title=str(data.get("title", data["id"])),
            branch=str(data["branch"]),
            prompt=str(data.get("prompt", "")),
            depends_on=deps,
            ladder=str(data.get("ladder", "default")),
            size=str(data.get("size", "medium")),
            timeout_minutes=int(data.get("timeout_minutes", 180)),
            auto_merge=bool(data.get("auto_merge", True)),
            release_after_merge=bool(data.get("release_after_merge", False)),
            force_rerun=bool(data.get("force_rerun", False)),
            position=int(data.get("position", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "branch": self.branch,
            "prompt": self.prompt,
            "depends_on": list(self.depends_on),
            "depends_on_json": json.dumps(self.depends_on),
            "ladder": self.ladder,
            "size": self.size,
            "timeout_minutes": self.timeout_minutes,
            "auto_merge": int(self.auto_merge),
            "release_after_merge": int(self.release_after_merge),
            "force_rerun": int(self.force_rerun),
            "position": self.position,
        }


def validate_plan_dag(phases: list[PhaseDefinition]) -> list[PhaseDefinition]:
    """Validate that phases form an acyclic directed graph and return topological order."""
    phase_by_id = {p.id: p for p in phases}
    if len(phase_by_id) != len(phases):
        raise ValueError("Each phase needs a unique ID.")
    for p in phases:
        if not p.branch.strip():
            raise ValueError(f"Phase '{p.id}' needs a branch.")
        for dep in p.depends_on:
            if dep not in phase_by_id:
                raise ValueError(f"Phase '{p.id}' depends on non-existent phase '{dep}'")

    # In-degree count and adjacency list for Kahn's algorithm
    in_degree: dict[str, int] = {p.id: 0 for p in phases}
    adj: dict[str, list[str]] = {p.id: [] for p in phases}
    for p in phases:
        for dep in p.depends_on:
            adj[dep].append(p.id)
            in_degree[p.id] += 1

    # Queue of nodes with in-degree 0
    # Use position / list order to break ties deterministically
    pos_map = {p.id: idx for idx, p in enumerate(phases)}
    ready = [p.id for p in phases if in_degree[p.id] == 0]
    ready.sort(key=lambda pid: (phases[pos_map[pid]].position, pos_map[pid]))

    ordered_ids: list[str] = []
    while ready:
        curr = ready.pop(0)
        ordered_ids.append(curr)
        for nxt in adj[curr]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=lambda pid: (phases[pos_map[pid]].position, pos_map[pid]))

    if len(ordered_ids) != len(phases):
        unresolved = [p.id for p in phases if in_degree[p.id] > 0]
        raise ValueError(f"Cyclic dependency detected in plan DAG among phases: {unresolved}")

    return [phase_by_id[pid] for pid in ordered_ids]


def get_runnable_phases(
    phases: list[PhaseDefinition],
    completed_phase_ids: set[str],
    parked_phase_ids: set[str] | None = None,
    running_phase_ids: set[str] | None = None,
) -> list[PhaseDefinition]:
    """Return all phases whose dependencies are satisfied."""
    parked = parked_phase_ids or set()
    running = running_phase_ids or set()
    runnable: list[PhaseDefinition] = []

    for phase in phases:
        if phase.id in completed_phase_ids or phase.id in running:
            continue
        # Direct dependencies satisfied by completed phases
        # or stacked on parked phases
        all_deps_met = True
        for dep in phase.depends_on:
            if dep not in completed_phase_ids and dep not in parked:
                all_deps_met = False
                break
        if all_deps_met:
            runnable.append(phase)

    return runnable


def resolve_base_branch(
    phase: PhaseDefinition,
    phases_by_id: dict[str, PhaseDefinition],
    parked_ids: set[str],
    default_base: str = "main",
) -> str:
    """Determine git base branch. If any parent phase is parked, stack onto that parked branch."""
    for dep in phase.depends_on:
        if dep in parked_ids and dep in phases_by_id:
            # Stacked PR: base is the parked branch per BLUEPRINT 5d
            return phases_by_id[dep].branch
    return default_base
