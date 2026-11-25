"""Morning digest and Mission Control deck data generator (BLUEPRINT 5e)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nightshift.store import Store


def generate_deck_data(
    store: Store,
    project_id: str,
    *,
    current_phase_title: str | None = None,
    current_step: str | None = None,
    current_engine: str | None = None,
    eta_formatted: str | None = None,
) -> dict[str, Any]:
    """Generate high-signal summary payload for Mission Control deck and phone bridge."""
    proj = store.get_project(project_id)
    proj_name = proj.get("name", project_id) if proj else project_id

    # 1. Inspect parked phases and alerts for 'needs_you'
    parked = store.list_parked()
    needs_you: list[dict[str, str]] = []
    for p in parked[:3]:
        needs_you.append({
            "phase_id": str(p["phase_id"]),
            "headline": f"Phase {p['phase_id']} is parked",
            "detail": str(p.get("reason", "Needs attention")),
            "action": str(p.get("next_step", "Inspect logs and resume")),
        })

    # 2. Phases status for 'done_tonight' and 'next'
    all_phases = store.list_phases()
    done_tonight: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []

    for ph in all_phases:
        st = ph.get("status", "pending")
        if st in ("merged", "completed"):
            steps = store.list_steps(ph["id"])
            runners = list({s["runner"] for s in steps if s.get("runner")})
            done_tonight.append({
                "phase_id": ph["id"],
                "title": ph.get("title", ph["id"]),
                "status": st,
                "runners": runners,
                "pr_number": ph.get("pr_number"),
            })
        elif st in ("pending", "queued"):
            upcoming.append({
                "phase_id": ph["id"],
                "title": ph.get("title", ph["id"]),
                "status": st,
            })

    # 3. Usage tonight per engine
    ledger = store.get_ledger_entries(limit=200)
    engine_usage: dict[str, dict[str, int]] = {}
    for entry in ledger:
        eng = entry.get("runner", "unknown")
        if eng not in engine_usage:
            engine_usage[eng] = {"invocations": 0, "tokens": 0}
        engine_usage[eng]["invocations"] += 1
        engine_usage[eng]["tokens"] += int(entry.get("tokens_in", 0)) + int(entry.get("tokens_out", 0))

    # 4. Handoffs count
    handoffs = store.list_handoffs()
    handoff_count = len(handoffs)

    # 5. One-line plain English headline
    if needs_you:
        headline = f"{len(needs_you)} phase(s) need your attention · {len(done_tonight)} completed tonight"
        state = "attention"
    elif current_phase_title:
        eta_part = f" · {eta_formatted}" if eta_formatted else ""
        headline = f"Working on {current_phase_title}{eta_part}"
        state = "working"
    elif done_tonight and not upcoming:
        headline = f"All {len(done_tonight)} phases completed cleanly tonight"
        state = "good"
    else:
        headline = f"{proj_name} is ready for execution"
        state = "idle"

    return {
        "project_id": project_id,
        "project_name": proj_name,
        "state": state,
        "headline": headline,
        "needs_you": needs_you,
        "done_tonight": done_tonight,
        "now": {
            "phase": current_phase_title,
            "step": current_step,
            "engine": current_engine,
            "eta": eta_formatted,
        },
        "next": upcoming[:3],
        "usage_tonight": engine_usage,
        "handoff_count": handoff_count,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_morning_digest(
    output_dir: Path,
    project_name: str,
    summary_rows: list[dict[str, Any]],
    deck: dict[str, Any] | None = None,
) -> Path:
    """Write human-readable MORNING_DIGEST.md to the project output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    digest_path = output_dir / "MORNING_DIGEST.md"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"# {project_name}: Continuous Delivery Digest ({now_str})",
        "",
        "## Summary",
    ]

    if deck:
        lines.append(f"**Status**: {deck.get('headline', 'Continuous run completed.')}\n")
        if deck.get("needs_you"):
            lines.append("### Attention Required")
            for item in deck["needs_you"]:
                lines.append(f"- **{item['headline']}**: {item['detail']} -> *Action: {item['action']}*")
            lines.append("")

    lines.extend([
        "## Phase Breakdown",
        "",
        "| Phase | Engine(s) | Status | Details |",
        "| :--- | :--- | :--- | :--- |",
    ])

    for r in summary_rows:
        title = r.get("title", r.get("phase_id", "Phase"))
        runner = r.get("runner", "auto")
        status = r.get("status", "UNKNOWN")
        detail = r.get("detail", "")
        lines.append(f"| {title} | `{runner}` | **{status}** | {detail} |")

    lines.append("")
    if deck and deck.get("usage_tonight"):
        lines.append("## AI Usage Tonight")
        for eng, stats in deck["usage_tonight"].items():
            lines.append(f"- **{eng}**: {stats['invocations']} run(s), ~{stats['tokens']} tokens")
        if deck.get("handoff_count"):
            lines.append(f"- **Handoffs triggered**: {deck['handoff_count']}")
        lines.append("")

    digest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return digest_path
