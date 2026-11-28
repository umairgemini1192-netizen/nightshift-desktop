"""Morning digest and Mission Control deck data generator (BLUEPRINT 5e)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nightshift.optimizer.ab_framework import ABFramework
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

    # 1. Inspect parked phases, blocked pipeline tasks, and failed release smoke tests for 'needs_you'
    parked = store.list_parked()
    needs_you: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    proj_repo = str(proj.get("repo") or "").strip() if proj else ""

    for task in store.list_tasks(status="BLOCKED"):
        if len(needs_you) >= 3:
            break
        block_reason = str(task.get("block_reason") or "")
        phase_id = str(task.get("phase_id") or task.get("id") or "")
        if block_reason == "awaiting_owner_approval":
            risk_class = str(task.get("risk_class") or "high_risk")
            risk_label = risk_class.replace("_", " ").capitalize()
            pr_num = task.get("pr_number")
            pr_str = f" PR #{pr_num}" if pr_num else ""
            head_sha = str(task.get("head_sha") or "").strip()
            if head_sha:
                item: dict[str, Any] = {
                    "phase_id": phase_id,
                    "task_id": str(task["id"]),
                    "head_sha": head_sha,
                    "risk_class": risk_class,
                    "headline": f"{risk_label}{pr_str} needs your approval before merge",
                    "detail": str(task.get("block_detail") or "Explicit owner approval is required for this risk class."),
                    "action": "Review & Approve",
                    "action_kind": "approve_task",
                }
                if pr_num is not None:
                    item["pr_number"] = int(pr_num)
            else:
                item = {
                    "phase_id": phase_id,
                    "task_id": str(task["id"]),
                    "risk_class": risk_class,
                    "headline": f"{risk_label}{pr_str} needs your approval before merge",
                    "detail": str(task.get("block_detail") or "Commit SHA is missing; inspect run logs before approving."),
                    "action": "Inspect runs",
                    "action_kind": "open_runs",
                }
            needs_you.append(item)
            seen_ids.add(phase_id)
        elif block_reason in ("smoke_test_failed", "smoke_test_unavailable"):
            tag = str(task.get("release_tag") or "").strip()
            tag_str = f" ({tag})" if tag else ""
            rel_rec = store.get_release_verification(proj_repo, tag) if (proj_repo and tag) else None
            if block_reason == "smoke_test_failed" and rel_rec and str(rel_rec.get("smoke_status") or "") == "rolled_back":
                if proj_repo and tag:
                    seen_ids.add(f"release:{proj_repo}:{tag}")
                seen_ids.add(phase_id)
                continue
            prev_tag = str(rel_rec.get("previous_good_tag") or "").strip() if rel_rec else ""
            can_rollback = bool(block_reason == "smoke_test_failed" and proj_repo and tag and prev_tag)
            item = {
                "phase_id": phase_id,
                "task_id": str(task["id"]),
                "project_id": project_id,
                "headline": f"Release smoke test blocked{tag_str}",
                "detail": str(task.get("block_detail") or "Post-release verification did not pass."),
                "action": "Roll back release" if can_rollback else "Configure smoke test",
                "action_kind": "rollback_release" if can_rollback else "configure_smoke_test",
            }
            if proj_repo:
                item["repo"] = proj_repo
            if tag:
                item["release_tag"] = tag
            if prev_tag:
                item["previous_good_tag"] = prev_tag
            needs_you.append(item)
            seen_ids.add(phase_id)
            if proj_repo and tag:
                seen_ids.add(f"release:{proj_repo}:{tag}")

    for rel in store.list_release_verifications(repo=proj_repo or None, limit=5):
        if len(needs_you) >= 3:
            break
        if str(rel.get("smoke_status") or "") == "failed":
            rel_key = f"release:{rel['repo']}:{rel['tag']}"
            if rel_key not in seen_ids:
                prev_tag = str(rel.get("previous_good_tag") or "").strip()
                detail = str(rel.get("smoke_detail") or "Post-release smoke test failed.")
                if prev_tag:
                    detail += f" Previous good release: {prev_tag}."
                item = {
                    "phase_id": rel_key,
                    "project_id": project_id,
                    "repo": str(rel["repo"]),
                    "release_tag": str(rel["tag"]),
                    "headline": f"Release {rel['tag']} failed launch smoke test",
                    "detail": detail,
                    "action": "Roll back release" if prev_tag else "Configure smoke test",
                    "action_kind": "rollback_release" if prev_tag else "configure_smoke_test",
                }
                if prev_tag:
                    item["previous_good_tag"] = prev_tag
                needs_you.append(item)
                seen_ids.add(rel_key)

    for p in parked:
        if len(needs_you) >= 3:
            break
        pid = str(p["phase_id"])
        if pid in seen_ids:
            continue
        needs_you.append({
            "phase_id": pid,
            "headline": f"Phase {p['phase_id']} is parked",
            "detail": str(p.get("reason", "Needs attention")),
            "action": str(p.get("next_step", "Inspect logs and resume")),
            "action_kind": "open_runs",
        })
        seen_ids.add(pid)

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

    # 4b. Gold saved tonight (estimate) (BLUEPRINT 5h)
    stats = store.get_engine_stats()
    economy_engines = {e["id"] for e in store.list_engines() if e.get("cost_tier") == "economy"}
    economy_successes = sum(s.get("successes", 0) for s in stats if s.get("engine") in economy_engines)
    gold_saved_dollars = round(economy_successes * 0.30, 2)
    gold_saved_tokens = economy_successes * 15000
    gold_saved_tonight = {
        "jobs_done": economy_successes,
        "cost_saved_usd": gold_saved_dollars,
        "tokens_saved": gold_saved_tokens,
        "estimate_label": f"${gold_saved_dollars} gold saved tonight (estimate)",
    }

    # 4c. Token optimizer savings tonight (estimate) and warnings (BLUEPRINT 5g)
    token_savings_tonight = ABFramework(store).summarize_tonight()
    for warn in token_savings_tonight.get("warnings", []):
        if len(needs_you) >= 3:
            break
        needs_you.append({
            "phase_id": "token_optimizer",
            "headline": "Token optimizer warning",
            "detail": str(warn),
            "action": "Open Settings",
            "action_kind": "open_settings",
        })

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
        "gold_saved_tonight": gold_saved_tonight,
        "token_savings_tonight": token_savings_tonight,
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

    if deck and deck.get("token_savings_tonight"):
        ts = deck["token_savings_tonight"]
        lines.append("## Token Optimizer Savings (Estimate)")
        lines.append(f"- **Tonight total**: {ts.get('estimateLabel', '~0 tokens saved tonight (estimate)')}")
        for item in ts.get("perTool", []):
            status_note = " (auto-disabled)" if item.get("autoDisabled") else ""
            lines.append(f"- **{item['tool']}**{status_note}: {item.get('estimateLabel', '~0 tokens saved (estimate)')}")
        for warn in ts.get("warnings", []):
            lines.append(f"- **Note**: {warn}")
        lines.append("")

    digest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return digest_path
