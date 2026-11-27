"""Trustworthy verification, evidence integrity, and mechanical PR body templating.

Implements Governance Amendment F and docs/AGENT_PIPELINE.md sections 2 (Stage 3 & 7) and 9.1.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.store import Store

DEFAULT_MIN_GATE_DURATION_S: float = 5.0
DEFAULT_MIN_BUILD_DURATION_S: float = 15.0


@dataclass(frozen=True)
class CiJobCheck:
    """Authoritative CI job status from CircleCI / CI API."""

    name: str
    status: str  # "success" | "failed" | "skipped" | "running"
    commit_sha: str
    build_id: str
    duration_seconds: float
    artifacts: list[str] = field(default_factory=list)
    compat_mode: bool = False
    is_gate_job: bool = False


@dataclass(frozen=True)
class EvidenceValidationResult:
    verified: bool
    rejection_reason: str = ""
    details: list[str] = field(default_factory=list)


def validate_ci_evidence(
    *,
    expected_commit_sha: str,
    required_jobs: list[str],
    observed_jobs: list[CiJobCheck],
    required_artifacts_by_job: dict[str, list[str]] | None = None,
    expected_base_sha: str | None = None,
    current_base_sha: str | None = None,
    min_gate_duration_s: float = DEFAULT_MIN_GATE_DURATION_S,
    min_build_duration_s: float = DEFAULT_MIN_BUILD_DURATION_S,
) -> EvidenceValidationResult:
    """Verify CI jobs ran on the exact commit SHA with real duration and required artifacts.

    Rejects zero-duration, skipped, or compat-mode passthrough greens.
    """
    details: list[str] = []
    if expected_base_sha and current_base_sha and expected_base_sha != current_base_sha:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="base_branch_drifted",
            details=[f"Base branch moved from {expected_base_sha[:7]} to {current_base_sha[:7]}."],
        )

    by_name = {j.name: j for j in observed_jobs}
    req_artifacts = required_artifacts_by_job or {}

    for job_name in required_jobs:
        job = by_name.get(job_name)
        if job is None:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="missing_required_job",
                details=[f"Required CI job '{job_name}' was not found."],
            )
        if job.commit_sha != expected_commit_sha:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="stale_commit_sha",
                details=[
                    f"Job '{job_name}' ran on {job.commit_sha[:7]}, not expected {expected_commit_sha[:7]}."
                ],
            )
        if job.compat_mode or job.status.lower() == "skipped":
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="suspicious_zero_duration_check",
                details=[f"Job '{job_name}' was skipped or ran in compat-mode passthrough."],
            )
        if job.status.lower() != "success":
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="ci_job_failed",
                details=[f"Job '{job_name}' status is '{job.status}', expected 'success'."],
            )
        min_dur = min_gate_duration_s if job.is_gate_job else min_build_duration_s
        if job.duration_seconds < min_dur:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="suspicious_zero_duration_check",
                details=[
                    f"Job '{job_name}' duration {job.duration_seconds:.1f}s is below minimum {min_dur:.1f}s."
                ],
            )
        expected_arts = req_artifacts.get(job_name, [])
        for art in expected_arts:
            if not any(art in actual for actual in job.artifacts):
                return EvidenceValidationResult(
                    verified=False,
                    rejection_reason="missing_required_artifact",
                    details=[f"Job '{job_name}' is missing required artifact '{art}'."],
                )
        details.append(f"{job_name}: ok ({job.duration_seconds:.1f}s, build {job.build_id})")

    return EvidenceValidationResult(verified=True, details=details)


def validate_screenshot_manifest(
    manifest_path: Path,
    *,
    expected_commit_sha: str,
    expected_build_id: str | None = None,
    screenshots_root: Path | None = None,
) -> EvidenceValidationResult:
    """Validate machine-written ``screenshot-manifest.json`` from a controlled renderer/emulator."""
    if not manifest_path.exists():
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="missing_screenshot_manifest",
            details=[f"Manifest not found: {manifest_path}"],
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="invalid_screenshot_manifest",
            details=[f"Could not parse manifest JSON: {exc}"],
        )

    if str(data.get("commit_sha") or "") != expected_commit_sha:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="stale_screenshot_commit",
            details=[f"Manifest commit_sha {data.get('commit_sha')} != {expected_commit_sha}."],
        )
    build_id = str(data.get("build_id") or "")
    if not build_id or (expected_build_id and build_id != expected_build_id):
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="invalid_screenshot_build_id",
            details=[f"Manifest build_id '{build_id}' does not match expected '{expected_build_id}'."],
        )

    entries = data.get("screenshots") or []
    if not entries:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="empty_screenshot_manifest",
            details=["Manifest contains zero screenshot entries."],
        )

    base_dir = screenshots_root or manifest_path.parent
    details: list[str] = []
    for entry in entries:
        file_rel = str(entry.get("file") or "")
        vp = str(entry.get("viewport_or_device") or "")
        axe_v = int(entry.get("axe_violations", -1))
        overflow = bool(entry.get("overflow", True))
        expected_sha = str(entry.get("sha256") or "")

        if not file_rel or not vp:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="incomplete_screenshot_entry",
                details=[f"Screenshot entry missing file or viewport_or_device: {entry}"],
            )
        if axe_v != 0:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="axe_violations_present",
                details=[f"Screenshot '{file_rel}' reported {axe_v} axe accessibility violations."],
            )
        if overflow:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="horizontal_overflow_detected",
                details=[f"Screenshot '{file_rel}' reported horizontal overflow."],
            )
        png_path = base_dir / file_rel
        if not png_path.exists():
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="missing_screenshot_file",
                details=[f"Screenshot file listed in manifest does not exist: {file_rel}"],
            )
        actual_sha = hashlib.sha256(png_path.read_bytes()).hexdigest()
        if not expected_sha or actual_sha != expected_sha:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="screenshot_sha256_mismatch",
                details=[f"SHA-256 mismatch for '{file_rel}': {actual_sha[:12]} != {expected_sha[:12]}."],
            )
        details.append(f"{file_rel} ({vp}, axe=0, overflow=false)")

    return EvidenceValidationResult(verified=True, details=details)


def persist_ci_evidence(
    store: Store,
    *,
    task_id: str,
    contract_sha256: str,
    commit_sha: str,
    kind: str,  # "ci_check" or "post_merge_ci"
    observed_jobs: list[CiJobCheck],
    validation: EvidenceValidationResult,
) -> list[int]:
    """Persist per-job evidence records to SQLite."""
    ids: list[int] = []
    for job in observed_jobs:
        rec_id = store.record_evidence(
            task_id=task_id,
            contract_sha256=contract_sha256,
            commit_sha=commit_sha,
            kind=kind,
            source=f"circleci:{job.name}",
            build_id=job.build_id,
            duration_seconds=job.duration_seconds,
            artifact_paths=job.artifacts,
            metadata={"status": job.status, "compat_mode": job.compat_mode},
            verified=validation.verified and job.status.lower() == "success",
            rejection_reason="" if validation.verified else validation.rejection_reason,
        )
        ids.append(rec_id)
    return ids


def render_mechanical_pr_body(
    *,
    task: dict[str, Any],
    contract: dict[str, Any],
    changed_files: list[str],
    evidence_records: list[dict[str, Any]],
    screenshots: list[str] | None = None,
    undone_notes: list[str] | None = None,
) -> str:
    """Generate a deterministic PR body from real verified data (Stage 3 MECHANICAL, no AI).

    Note: Per Amendment F, this PR body is a human summary only and is NEVER parsed as gate evidence.
    """
    criteria = json.loads(str(contract.get("acceptance_criteria_json") or "[]"))
    lines = [
        f"## Task: {task.get('title', task.get('id', 'Unknown'))}",
        "",
        f"- **Task ID:** `{task.get('id', '')}`",
        f"- **Risk class:** `{task.get('risk_class', contract.get('risk_class', 'business_logic'))}`",
        f"- **Contract SHA-256:** `{str(contract.get('contract_sha256', ''))[:16]}`",
        f"- **Head SHA:** `{str(task.get('head_sha') or 'pending')[:12]}`",
        "",
        "### Changed files",
    ]
    if changed_files:
        for path in changed_files:
            lines.append(f"- `{path}`")
    else:
        lines.append("- None recorded yet")

    lines.extend(["", "### Acceptance criteria"])
    if criteria:
        for ac in criteria:
            lines.append(f"- `{ac.get('id', 'ac')}`: {ac.get('description', '')}")
    else:
        lines.append("- None specified")

    lines.extend(["", "### Verified checks (authoritative records)"])
    if evidence_records:
        for rec in evidence_records:
            status = "PASS" if rec.get("verified") else f"REJECTED ({rec.get('rejection_reason', 'failed')})"
            lines.append(
                f"- `{rec.get('source', rec.get('kind'))}`: {status} "
                f"({float(rec.get('duration_seconds', 0.0)):.1f}s, commit `{str(rec.get('commit_sha', ''))[:7]}`)"
            )
    else:
        lines.append("- Pending CI verification")

    lines.extend(["", "### Screenshots"])
    if screenshots:
        for shot in screenshots:
            lines.append(f"- `{shot}`")
    else:
        lines.append("- None")

    lines.extend(["", "### Left undone"])
    if undone_notes:
        for note in undone_notes:
            lines.append(f"- {note}")
    else:
        lines.append("- Nothing left undone in this task.")

    return "\n".join(lines) + "\n"
