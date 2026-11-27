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
    allowed_globs: list[str] = field(default_factory=list)
    forbidden_globs: list[str] | None = None
    risk_class: str = "business_logic"
    owner_approval_required: bool = False

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
            "auto_merge": self.auto_merge,
            "release_after_merge": self.release_after_merge,
            "force_rerun": self.force_rerun,
            "position": self.position,
        }


KNOWN_LADDERS: set[str] = {"backend", "ui", "review", "ci_fix", "docs", "test", "default"}


def validate_plan(
    phases: list[PhaseDefinition],
    known_ladders: set[str] | None = None,
) -> list[str]:
    """Validate a plan and return plain-English problems. Returns empty list if valid."""
    problems: list[str] = []
    if not phases:
        return ["The plan has no phases to run."]

    ladders = known_ladders if known_ladders is not None else KNOWN_LADDERS
    seen_ids: set[str] = set()
    phase_by_id: dict[str, PhaseDefinition] = {}

    for p in phases:
        if not p.id.strip():
            problems.append("Every phase needs a valid ID.")
        elif p.id in seen_ids:
            problems.append(f"Duplicate phase ID '{p.id}'. Each phase needs a unique ID.")
        else:
            seen_ids.add(p.id)
            phase_by_id[p.id] = p

        if not p.branch.strip():
            problems.append(f"Phase '{p.id}' needs a branch.")

        if ladders and p.ladder not in ladders:
            problems.append(f"Phase '{p.id}' specifies unknown ladder '{p.ladder}'.")

    for p in phases:
        for dep in p.depends_on:
            if dep not in phase_by_id:
                problems.append(f"Phase '{p.id}' depends on non-existent phase '{dep}'.")

    # Cycle detection via Kahn's algorithm
    if not any("Duplicate phase ID" in prob or "non-existent" in prob for prob in problems):
        in_degree: dict[str, int] = {p.id: 0 for p in phases}
        adj: dict[str, list[str]] = {p.id: [] for p in phases}
        for p in phases:
            for dep in p.depends_on:
                adj[dep].append(p.id)
                in_degree[p.id] += 1

        ready = [p.id for p in phases if in_degree[p.id] == 0]
        visited_count = 0
        while ready:
            curr = ready.pop(0)
            visited_count += 1
            for nxt in adj[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    ready.append(nxt)

        if visited_count < len(phases):
            unresolved = [p.id for p in phases if in_degree[p.id] > 0]
            problems.append(
                f"Cyclic dependency detected in plan DAG among phases: {', '.join(unresolved)}."
            )

    return problems


def validate_plan_dag(phases: list[PhaseDefinition], known_ladders: set[str] | None = None) -> list[PhaseDefinition]:
    """Validate that phases form an acyclic directed graph and return topological order."""
    problems = validate_plan(phases, known_ladders=known_ladders)
    if problems:
        raise ValueError(problems[0])

    phase_by_id = {p.id: p for p in phases}
    in_degree: dict[str, int] = {p.id: 0 for p in phases}
    adj: dict[str, list[str]] = {p.id: [] for p in phases}
    for p in phases:
        for dep in p.depends_on:
            adj[dep].append(p.id)
            in_degree[p.id] += 1

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

    return [phase_by_id[pid] for pid in ordered_ids]


def import_queue_json(data_or_str: str | dict[str, Any]) -> dict[str, Any]:
    """Parse legacy queue.json format into project, plan, phases, ladders, and settings."""
    if isinstance(data_or_str, str):
        clean = data_or_str.strip()
        if clean.startswith("\ufeff"):
            clean = clean[1:]
        cfg = json.loads(clean)
    else:
        cfg = dict(data_or_str)

    repo = str(cfg.get("repo", "")).strip()
    project_id = repo or "imported-project"

    project = {
        "id": project_id,
        "name": str(cfg.get("name") or project_id),
        "repo": repo,
        "local_path": str(cfg.get("local_repo", "")),
        "output_dir": str(cfg.get("desktop_apk_dir", "")),
        "output_folder": str(cfg.get("desktop_apk_dir", "")),
        "build_rules": str(cfg.get("build_rules", "")),
        "artifact_glob": str(cfg.get("artifact_glob", "*.apk")),
    }

    plan_id = str(cfg.get("plan_id") or f"plan_{project_id}")
    plan = {
        "id": plan_id,
        "project_id": project_id,
        "title": str(cfg.get("title") or f"Queue Plan for {repo or project_id}"),
        "goal": str(cfg.get("goal", "")),
        "status": "draft",
    }

    ladders = cfg.get("failover_ladders", {})
    if not isinstance(ladders, dict):
        ladders = {}

    settings = {
        "auto_merge": bool(cfg.get("auto_merge", True)),
        "auto_release": bool(cfg.get("auto_release", True)),
        "max_ci_retries": int(cfg.get("max_ci_retries", 2)),
        "quota_cooldown_minutes": int(cfg.get("quota_cooldown_minutes", 60)),
        "runner_timeout_minutes": int(cfg.get("runner_timeout_minutes", 90)),
    }

    raw_phases = cfg.get("phases", [])
    phases: list[PhaseDefinition] = []
    for idx, p in enumerate(raw_phases):
        pid = str(p.get("id") or f"p{idx + 1}")
        title = str(p.get("title") or f"Phase {idx + 1}")
        branch = str(p.get("branch") or f"feat/{pid}")
        prompt = str(p.get("prompt", ""))
        deps = list(p.get("depends_on", []))
        ladder = str(p.get("ladder", "default"))
        size = str(p.get("size", "medium"))
        timeout = int(p.get("timeout_minutes", settings["runner_timeout_minutes"]))
        auto_m = bool(p.get("auto_merge", settings["auto_merge"]))
        rel = bool(p.get("release_after_merge", settings["auto_release"]))
        rerun = bool(p.get("force_rerun", False))

        phases.append(
            PhaseDefinition(
                id=pid,
                title=title,
                branch=branch,
                prompt=prompt,
                depends_on=deps,
                ladder=ladder,
                size=size,
                timeout_minutes=timeout,
                auto_merge=auto_m,
                release_after_merge=rel,
                force_rerun=rerun,
                position=idx,
            )
        )

    return {
        "project": project,
        "plan": plan,
        "phases": phases,
        "ladders": ladders,
        "settings": settings,
    }


def export_queue_json(
    project: dict[str, Any],
    plan: dict[str, Any],
    phases: list[PhaseDefinition | dict[str, Any]],
    ladders: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export project, plan, and phases into legacy queue.json format."""
    st = settings or {}
    serialized_phases = []
    for idx, p in enumerate(phases):
        if isinstance(p, PhaseDefinition):
            p_dict = p.to_dict()
        else:
            p_dict = dict(p)

        raw_deps = p_dict.get("depends_on") or p_dict.get("depends_on_json") or []
        if isinstance(raw_deps, str):
            try:
                deps = json.loads(raw_deps)
            except Exception:
                deps = []
        else:
            deps = list(raw_deps)

        phase_entry: dict[str, Any] = {
            "id": str(p_dict.get("id", f"p{idx+1}")),
            "title": str(p_dict.get("title", "")),
            "branch": str(p_dict.get("branch", "")),
            "prompt": str(p_dict.get("prompt", "")),
            "depends_on": deps,
            "ladder": str(p_dict.get("ladder", "default")),
            "size": str(p_dict.get("size", "medium")),
            "timeout_minutes": int(p_dict.get("timeout_minutes", 180)),
            "auto_merge": bool(p_dict.get("auto_merge", True)),
            "release_after_merge": bool(p_dict.get("release_after_merge", False)),
            "force_rerun": bool(p_dict.get("force_rerun", False)),
        }
        if p_dict.get("implementer"):
            phase_entry["implementer"] = str(p_dict["implementer"])
        serialized_phases.append(phase_entry)

    local_repo = str(project.get("local_path") or project.get("local_repo") or "")
    desktop_apk_dir = str(project.get("output_dir") or project.get("output_folder") or project.get("desktop_apk_dir") or "")
    artifact_glob = str(project.get("artifact_glob") or "*.apk")

    return {
        "repo": str(project.get("repo", "")),
        "local_repo": local_repo,
        "desktop_apk_dir": desktop_apk_dir,
        "artifact_glob": artifact_glob,
        "auto_merge": bool(st.get("auto_merge", True)),
        "auto_release": bool(st.get("auto_release", True)),
        "max_ci_retries": int(st.get("max_ci_retries", 2)),
        "quota_cooldown_minutes": int(st.get("quota_cooldown_minutes", 60)),
        "runner_timeout_minutes": int(st.get("runner_timeout_minutes", 90)),
        "failover_ladders": dict(ladders or {}),
        "phases": serialized_phases,
    }


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
