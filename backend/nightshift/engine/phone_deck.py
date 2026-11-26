"""Build a deliberately restricted, project-scoped phone status deck."""

from __future__ import annotations

import re
import statistics
from typing import Any

from nightshift.engine.verify import redact_secrets
from nightshift.store import Store

ACTIVE_STATUSES = frozenset({
    "running",
    "working",
    "implementing",
    "watching_ci",
    "merging",
    "preparing_worktree",
    "verifying_build",
    "releasing",
})


def _label(value: Any, limit: int = 100) -> str:
    """Keep labels to one line and remove path-like text before redaction."""
    text = str(value or "").splitlines()[0] if value else ""
    # Strip drive-letter paths (C:\..., D:/...), UNC paths (\\...), and absolute Unix paths (/foo/bar).
    text = re.sub(r"(?:[A-Za-z]:[\\/]|\\\\|(?<!\w)/)\S+", "[path removed]", text)
    # Strip relative file paths that carry a file extension (e.g. src/app/secret.py, backend\a.py).
    text = re.sub(r"\b(?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9]{1,8}\b", "[path removed]", text)
    return redact_secrets(text).strip()[:limit]


def _estimate_eta(store: Store, remaining_count: int, step_kind: str, runner: str) -> str:
    """Compute a compact, clearly labelled ETA estimate from duration history only."""
    if remaining_count <= 0:
        return ""
    step_sec = 1800.0
    avg_phase_sec = 2400.0
    if hasattr(store, "get_durations"):
        try:
            step_durs = store.get_durations(kind=step_kind or "implement", runner=runner or None)
            if step_durs:
                step_sec = float(statistics.median(step_durs))
            all_durs = store.get_durations()
            if len(all_durs) >= 3:
                avg_phase_sec = float(statistics.median(all_durs))
        except Exception:
            pass
    total_sec = step_sec + max(0, remaining_count - 1) * avg_phase_sec
    hours = int(total_sec // 3600)
    minutes = max(1, int((total_sec % 3600) // 60))
    if hours > 0:
        return f"~{hours}h {minutes}m (estimate)"
    return f"~{minutes}m (estimate)"


def build_phone_deck(store: Store, project_id: str) -> dict[str, Any]:
    """Project only safe summaries; never copy event, step, or parked detail text."""
    resolved_id = project_id
    if not resolved_id and hasattr(store, "list_projects"):
        projects = store.list_projects()
        if projects:
            resolved_id = str(projects[0].get("id") or "")
    project = (store.get_project(resolved_id) if resolved_id else None) or {}
    plans = store.list_plans(resolved_id) if resolved_id else []
    phases = store.list_phases(plans[-1]["id"]) if plans else []
    active = next((p for p in phases if p.get("status") in ACTIVE_STATUSES), None)
    parked = [p for p in phases if p.get("status") in {"parked", "failed"}]
    done = [p for p in phases if p.get("status") in {"merged", "completed"}]
    upcoming = [p for p in phases if p.get("status") in {"pending", "queued"}]
    state = "attention" if parked else "working" if active else "good" if done and not upcoming else "stopped"

    total = len(phases)
    done_count = len(done)
    if state == "attention":
        headline = f"Needs you - {len(parked)} phase(s) parked, {done_count} of {total} done"
    elif state == "working":
        headline = f"Working - {done_count} of {total} phases done"
    elif state == "good":
        headline = f"All good - {done_count} of {total} phases complete"
    elif total > 0:
        headline = f"Stopped - {done_count} of {total} phases done"
    else:
        headline = "No phase is running"

    now_step = ""
    now_engine = ""
    now_eta = ""
    if active:
        now_step = _label(active.get("status"), 40)
        if hasattr(store, "list_steps"):
            steps = store.list_steps(active["id"])
            if steps:
                latest = steps[-1]
                now_step = _label(latest.get("kind") or active.get("status"), 40)
                now_engine = _label(latest.get("runner"), 40)
        now_eta = _label(_estimate_eta(store, 1 + len(upcoming), now_step, now_engine), 40)

    return {
        "project": _label(project.get("name") or resolved_id or "nightshift", 60),
        "state": state,
        "headline": _label(headline, 140),
        "now": {
            "phase": _label(active.get("title"), 80) if active else "",
            "step": now_step,
            "engine": now_engine,
            "eta": now_eta,
        },
        "needs_you": [_label(p.get("title"), 120) for p in parked[:3]],
        "done_tonight": [_label(p.get("title"), 100) for p in done[:12]],
        "next": [_label(p.get("title"), 100) for p in upcoming[:3]],
        "phases": [{"title": _label(p.get("title"), 60), "state": _label(p.get("status"), 32)} for p in phases[:20]],
    }
