"""Build a deliberately restricted, project-scoped phone status deck."""

from __future__ import annotations

import re
from typing import Any

from nightshift.engine.verify import redact_secrets
from nightshift.store import Store


def _label(value: Any, limit: int = 100) -> str:
    """Keep labels to one line and remove path-like text before redaction."""
    text = str(value or "").splitlines()[0] if value else ""
    text = re.sub(r"(?:[A-Za-z]:[\\/]|/|\\\\)\S+", "[path removed]", text)
    return redact_secrets(text)[:limit]


def build_phone_deck(store: Store, project_id: str) -> dict[str, Any]:
    """Project only safe summaries; never copy event, step, or parked detail text."""
    project = store.get_project(project_id) or {}
    plans = store.list_plans(project_id)
    phases = store.list_phases(plans[-1]["id"]) if plans else []
    active = next((p for p in phases if p.get("status") in {"running", "working"}), None)
    parked = [p for p in phases if p.get("status") in {"parked", "failed"}]
    done = [p for p in phases if p.get("status") in {"merged", "completed"}]
    upcoming = [p for p in phases if p.get("status") in {"pending", "queued"}]
    state = "attention" if parked else "working" if active else "good" if done and not upcoming else "stopped"
    headlines = {
        "attention": "Some phases need your attention",
        "working": "A phase is in progress",
        "good": "All phases are complete",
        "stopped": "No phase is running",
    }
    return {
        "project": _label(project.get("name") or project_id),
        "state": state,
        "headline": headlines[state],
        "now": {"phase": _label(active.get("title")) if active else "", "step": "", "engine": "", "eta": ""},
        "needs_you": [_label(p.get("title")) for p in parked[:3]],
        "done_tonight": [_label(p.get("title")) for p in done],
        "next": [_label(p.get("title")) for p in upcoming[:3]],
        "phases": [{"title": _label(p.get("title")), "state": _label(p.get("status"), 32)} for p in phases],
    }
