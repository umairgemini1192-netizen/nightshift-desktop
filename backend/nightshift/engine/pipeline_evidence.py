"""Trustworthy verification, evidence integrity, and mechanical PR body templating.

Implements Governance Amendment F and docs/AGENT_PIPELINE.md sections 2 (Stage 3 & 7) and 9.1
(Phase 8d: Trustworthy verification & evidence integrity).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.store import Store

DEFAULT_MIN_GATE_DURATION_S: float = 5.0
DEFAULT_MIN_BUILD_DURATION_S: float = 15.0

DEFAULT_GATE_JOB_NAMES: frozenset[str] = frozenset(
    {
        "desktop-gates",
        "gates",
        "design-gates",
    }
)

DEFAULT_NIGHTSHIFT_REQUIRED_JOBS: tuple[str, ...] = (
    "desktop-gates",
    "desktop-backend",
    "desktop-frontend",
    "desktop-package",
)

ALLOWED_SCREENSHOT_RENDERERS: frozenset[str] = frozenset(
    {
        "playwright-mockbridge",
        "playwright-vite-preview",
        "android-emulator-ci",
    }
)

UNTRUSTED_EVIDENCE_SOURCE_PREFIXES: tuple[str, ...] = (
    "pr_body",
    "pr_description",
    "pr_comment",
    "commit_message",
    "self_report",
    "agent_claim",
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class CiArtifact:
    """Authoritative CI artifact attached to a specific CI build/pipeline."""

    path: str
    size_bytes: int = 0
    build_id: str = ""
    url: str = ""
    scope: str = "job"  # "job" (per-job, e.g. CircleCI) | "workflow_run" (workflow-wide, e.g. GitHub Actions)


@dataclass(frozen=True)
class CiJobCheck:
    """Authoritative CI job status from CircleCI / CI API."""

    name: str
    status: str  # "success" | "failed" | "skipped" | "running" | "canceled"
    commit_sha: str
    build_id: str
    duration_seconds: float
    artifacts: list[CiArtifact | str] = field(default_factory=list)
    compat_mode: bool = False
    is_gate_job: bool = False
    branch: str = ""
    workflow_run_id: str = ""
    workflow_run_job_count: int = 1


@dataclass(frozen=True)
class EvidenceValidationResult:
    """Result of validating authoritative CI or screenshot evidence."""

    verified: bool
    rejection_reason: str = ""
    details: list[str] = field(default_factory=list)
    job_results: dict[str, tuple[bool, str]] = field(default_factory=dict)


def _norm_posix(path: str) -> str:
    norm = str(path).strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def is_untrusted_evidence_source(source: str) -> bool:
    """Return True if ``source`` is a non-authoritative claim (e.g. PR body or comment)."""
    low = (source or "").strip().lower()
    if not low:
        return True
    return any(low == prefix or low.startswith(f"{prefix}:") or low.startswith(f"{prefix}/") for prefix in UNTRUSTED_EVIDENCE_SOURCE_PREFIXES)


def validate_evidence_record_trust(record: dict[str, Any]) -> tuple[bool, str]:
    """Verify a stored ``evidence_records`` row came from an authoritative source, never PR body."""
    source = str(record.get("source") or "")
    if is_untrusted_evidence_source(source):
        return False, f"Untrusted evidence source '{source}' (PR body / self-report is never evidence)."
    meta_raw = record.get("metadata_json") or "{}"
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else dict(meta_raw)
    except Exception:
        meta = {}
    if meta.get("from_pr_body") or meta.get("self_reported"):
        return False, "Evidence flagged as PR-body or self-reported is forbidden."
    return True, "Authoritative evidence source."


def _artifact_matches(actual_path: str, required_spec: str) -> bool:
    actual = _norm_posix(actual_path)
    req = _norm_posix(required_spec)
    if not actual or not req:
        return False
    if actual == req:
        return True
    req_dir = req.rstrip("/")
    if actual.startswith(req_dir + "/"):
        return True
    if actual.endswith("/" + req_dir):
        return True
    if fnmatch.fnmatchcase(actual, req):
        return True
    if "/" not in req and fnmatch.fnmatchcase(actual.rsplit("/", 1)[-1], req):
        return True
    return False


def _job_has_required_artifact(
    job: CiJobCheck,
    required_spec: str,
    *,
    observed_jobs: list[CiJobCheck] | None = None,
) -> tuple[bool, str]:
    """Verify ``job`` has a non-empty downloadable artifact matching ``required_spec`` with valid producer provenance."""
    found_empty = False
    found_wrong_build = False
    found_ambiguous_scope = False
    for raw_art in job.artifacts:
        if isinstance(raw_art, CiArtifact):
            art_path = raw_art.path
            size_bytes = raw_art.size_bytes
            art_build_id = raw_art.build_id
            art_scope = (raw_art.scope or "job").strip().lower()
        else:
            art_path = str(raw_art)
            size_bytes = 1  # Legacy string artifact entries represent non-empty artifact paths
            art_build_id = job.build_id
            art_scope = "job"

        if not _artifact_matches(art_path, required_spec):
            continue

        if art_scope == "workflow_run":
            expected_wf_build = f"workflow-run:{job.workflow_run_id}" if job.workflow_run_id else ""
            if not expected_wf_build or art_build_id != expected_wf_build:
                found_wrong_build = True
                continue
            observed_same_run = sum(
                1 for j in (observed_jobs or []) if j.workflow_run_id and j.workflow_run_id == job.workflow_run_id
            )
            if max(int(job.workflow_run_job_count or 1), observed_same_run) > 1:
                found_ambiguous_scope = True
                continue
        else:
            if not art_build_id or not job.build_id or art_build_id != job.build_id:
                found_wrong_build = True
                continue

        if size_bytes <= 0:
            found_empty = True
            continue
        return True, ""

    if found_empty:
        return False, f"Job '{job.name}' artifact matching '{required_spec}' is empty (0 bytes)."
    if found_ambiguous_scope:
        return (
            False,
            f"Job '{job.name}' artifact matching '{required_spec}' has workflow-run scope shared by multiple jobs; "
            f"job-specific producer provenance cannot be verified.",
        )
    if found_wrong_build:
        return (
            False,
            f"Job '{job.name}' artifact matching '{required_spec}' does not belong to build '{job.build_id}'.",
        )
    return False, f"Job '{job.name}' is missing required artifact '{required_spec}'."


def _select_observed_job(
    job_name: str,
    observed_jobs: list[CiJobCheck],
    expected_commit_sha: str,
) -> CiJobCheck | None:
    """Pick the most relevant observed run for ``job_name`` (preferring ``expected_commit_sha``)."""
    matching_name = [j for j in observed_jobs if j.name == job_name]
    if not matching_name:
        return None
    exact = [j for j in matching_name if j.commit_sha == expected_commit_sha]
    if exact:
        return exact[-1]
    return matching_name[-1]


def validate_ci_evidence(
    *,
    expected_commit_sha: str,
    required_jobs: list[str],
    observed_jobs: list[CiJobCheck],
    required_artifacts_by_job: dict[str, list[str]] | None = None,
    expected_base_sha: str | None = None,
    current_base_sha: str | None = None,
    require_base_sha_match: bool = False,
    expected_branch: str | None = None,
    min_gate_duration_s: float = DEFAULT_MIN_GATE_DURATION_S,
    min_build_duration_s: float = DEFAULT_MIN_BUILD_DURATION_S,
    min_duration_by_job: dict[str, float] | None = None,
    gate_job_names: frozenset[str] = DEFAULT_GATE_JOB_NAMES,
) -> EvidenceValidationResult:
    """Verify CI jobs ran on the exact commit SHA with real duration and required artifacts.

    Enforces Amendment F (AGENT_PIPELINE §9.1):
    - Exact commit SHA binding (rejects stale or missing SHA).
    - Race-safe base branch binding (rejects drifted ``expected_base_sha``).
    - Rejects zero-duration, sub-threshold, skipped, or compat-mode passthrough greens.
    - Enforces non-empty downloadable artifacts bound to the exact CI build ID.
    - Fails closed if ``required_jobs`` is empty.
    """
    if not expected_commit_sha or not expected_commit_sha.strip():
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="missing_commit_sha",
            details=["Expected commit SHA is required for CI evidence verification."],
        )

    if not required_jobs:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="no_required_jobs_configured",
            details=["Cannot verify CI evidence with an empty required_jobs list."],
        )

    if require_base_sha_match and (not expected_base_sha or not current_base_sha):
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="missing_base_sha",
            details=["Race-safe base branch verification requires both expected_base_sha and current_base_sha."],
        )

    if expected_base_sha and current_base_sha and expected_base_sha != current_base_sha:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="base_branch_drifted",
            details=[f"Base branch moved from {expected_base_sha[:7]} to {current_base_sha[:7]}."],
        )

    req_artifacts = required_artifacts_by_job or {}
    per_job_min = min_duration_by_job or {}
    details: list[str] = []
    job_results: dict[str, tuple[bool, str]] = {}
    first_failure_reason = ""
    failure_details: list[str] = []

    for job_name in required_jobs:
        job = _select_observed_job(job_name, observed_jobs, expected_commit_sha)
        if job is None:
            reason = "missing_required_job"
            msg = f"Required CI job '{job_name}' was not found."
            job_results[job_name] = (False, reason)
            failure_details.append(msg)
            first_failure_reason = first_failure_reason or reason
            continue

        if not job.commit_sha or job.commit_sha != expected_commit_sha:
            reason = "stale_commit_sha"
            msg = f"Job '{job_name}' ran on {job.commit_sha[:7] or 'none'}, not expected {expected_commit_sha[:7]}."
            job_results[job_name] = (False, reason)
            failure_details.append(msg)
            first_failure_reason = first_failure_reason or reason
            continue

        if expected_branch and job.branch and job.branch != expected_branch:
            reason = "wrong_branch"
            msg = f"Job '{job_name}' ran on branch '{job.branch}', expected '{expected_branch}'."
            job_results[job_name] = (False, reason)
            failure_details.append(msg)
            first_failure_reason = first_failure_reason or reason
            continue

        if not job.build_id or not str(job.build_id).strip():
            reason = "missing_build_id"
            msg = f"Job '{job_name}' is missing an authoritative CI build_id."
            job_results[job_name] = (False, reason)
            failure_details.append(msg)
            first_failure_reason = first_failure_reason or reason
            continue

        status_low = (job.status or "").strip().lower()
        if job.compat_mode or status_low == "skipped":
            reason = "suspicious_zero_duration_check"
            msg = f"Job '{job_name}' was skipped or ran in compat-mode passthrough."
            job_results[job_name] = (False, reason)
            failure_details.append(msg)
            first_failure_reason = first_failure_reason or reason
            continue

        if status_low != "success":
            reason = "ci_job_failed"
            msg = f"Job '{job_name}' status is '{job.status}', expected 'success'."
            job_results[job_name] = (False, reason)
            failure_details.append(msg)
            first_failure_reason = first_failure_reason or reason
            continue

        is_gate = job.is_gate_job or job_name in gate_job_names
        default_min = min_gate_duration_s if is_gate else min_build_duration_s
        min_dur = float(per_job_min.get(job_name, default_min))
        if (
            not isinstance(job.duration_seconds, (int, float))
            or math.isnan(job.duration_seconds)
            or math.isinf(job.duration_seconds)
            or job.duration_seconds <= 0.0
            or job.duration_seconds < min_dur
        ):
            reason = "suspicious_zero_duration_check"
            msg = f"Job '{job_name}' duration {job.duration_seconds:.1f}s is below minimum {min_dur:.1f}s."
            job_results[job_name] = (False, reason)
            failure_details.append(msg)
            first_failure_reason = first_failure_reason or reason
            continue

        expected_arts = req_artifacts.get(job_name, [])
        missing_art_msg = ""
        for art in expected_arts:
            ok_art, art_msg = _job_has_required_artifact(job, art, observed_jobs=observed_jobs)
            if not ok_art:
                missing_art_msg = art_msg
                break
        if missing_art_msg:
            reason = "missing_required_artifact"
            job_results[job_name] = (False, reason)
            failure_details.append(missing_art_msg)
            first_failure_reason = first_failure_reason or reason
            continue

        job_results[job_name] = (True, "")
        details.append(f"{job_name}: ok ({job.duration_seconds:.1f}s, build {job.build_id})")

    if first_failure_reason:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason=first_failure_reason,
            details=failure_details,
            job_results=job_results,
        )

    return EvidenceValidationResult(
        verified=True,
        details=details,
        job_results=job_results,
    )


def validate_screenshot_manifest(
    manifest_path: Path,
    *,
    expected_commit_sha: str,
    expected_build_id: str | None = None,
    screenshots_root: Path | None = None,
    required_viewports: list[str] | None = None,
    allowed_renderers: frozenset[str] = ALLOWED_SCREENSHOT_RENDERERS,
) -> EvidenceValidationResult:
    """Validate machine-written ``screenshot-manifest.json`` from a controlled renderer/emulator.

    Checks (AGENT_PIPELINE §9.1 item 4):
    - Manifest exists and is valid JSON object.
    - Produced by a controlled renderer/emulator (``playwright-mockbridge``, ``android-emulator-ci``, etc.).
    - ``build_id`` is non-empty (and matches ``expected_build_id`` if provided).
    - ``commit_sha`` matches ``expected_commit_sha``.
    - Non-empty ``screenshots`` array where each item records:
      ``file`` (safe relative PNG path inside ``screenshots_root``), ``viewport_or_device``, ``scale``,
      ``axe_violations == 0``, ``overflow == false``, and exact SHA-256 of the non-empty PNG file.
    """
    if not expected_commit_sha or not expected_commit_sha.strip():
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="missing_commit_sha",
            details=["Expected commit SHA is required to validate screenshot manifest."],
        )

    if not manifest_path.exists() or not manifest_path.is_file():
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="missing_screenshot_manifest",
            details=[f"Screenshot manifest not found at {manifest_path}."],
        )

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="invalid_screenshot_manifest",
            details=[f"Failed to parse screenshot manifest JSON: {exc}"],
        )

    if not isinstance(data, dict):
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="invalid_screenshot_manifest",
            details=["Screenshot manifest root must be a JSON object."],
        )

    renderer = str(data.get("renderer") or data.get("controlled_renderer") or "").strip()
    if renderer not in allowed_renderers:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="uncontrolled_screenshot_renderer",
            details=[
                f"Manifest renderer '{renderer or 'missing'}' is not in allowed controlled renderers "
                f"{sorted(allowed_renderers)}."
            ],
        )

    build_id = str(data.get("build_id") or "").strip()
    if not build_id:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="missing_build_id",
            details=["Screenshot manifest is missing build_id."],
        )
    if expected_build_id and build_id != expected_build_id:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="screenshot_build_id_mismatch",
            details=[f"Screenshot manifest build_id '{build_id}' != expected '{expected_build_id}'."],
        )

    commit_sha = str(data.get("commit_sha") or "").strip()
    if commit_sha != expected_commit_sha:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="screenshot_commit_sha_mismatch",
            details=[
                f"Screenshot manifest commit_sha '{commit_sha[:7] or 'missing'}' != expected '{expected_commit_sha[:7]}'."
            ],
        )

    entries = data.get("screenshots")
    if not isinstance(entries, list) or not entries:
        return EvidenceValidationResult(
            verified=False,
            rejection_reason="empty_screenshot_manifest",
            details=["Screenshot manifest contains no screenshot entries."],
        )

    base_dir = (screenshots_root or manifest_path.parent).resolve()
    details: list[str] = []
    seen_viewports: set[str] = set()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="incomplete_screenshot_entry",
                details=[f"Screenshot entry #{idx} is not a JSON object."],
            )
        file_rel = str(entry.get("file") or "").strip()
        vp = str(entry.get("viewport_or_device") or "").strip()
        scale = entry.get("scale")
        axe_v = entry.get("axe_violations")
        overflow = entry.get("overflow")
        expected_sha = str(entry.get("sha256") or "").strip().lower()

        if not file_rel or not vp or scale is None or str(scale).strip() == "":
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="incomplete_screenshot_entry",
                details=[f"Screenshot entry #{idx} missing file, viewport_or_device, or scale."],
            )
        if not file_rel.lower().endswith(".png"):
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="invalid_screenshot_format",
                details=[f"Screenshot '{file_rel}' must be a PNG file."],
            )
        if isinstance(axe_v, bool) or not isinstance(axe_v, int):
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="incomplete_screenshot_entry",
                details=[f"Screenshot '{file_rel}' has invalid axe_violations value: {axe_v!r}."],
            )
        if axe_v != 0:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="axe_violations_present",
                details=[f"Screenshot '{file_rel}' reported {axe_v} axe accessibility violations."],
            )
        if not isinstance(overflow, bool):
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="incomplete_screenshot_entry",
                details=[f"Screenshot '{file_rel}' has non-boolean overflow value: {overflow!r}."],
            )
        if overflow:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="horizontal_overflow_detected",
                details=[f"Screenshot '{file_rel}' reported horizontal overflow."],
            )

        png_path = (base_dir / file_rel).resolve()
        try:
            png_path.relative_to(base_dir)
        except ValueError:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="screenshot_path_traversal",
                details=[f"Screenshot path '{file_rel}' escapes screenshots root."],
            )

        if not png_path.exists() or not png_path.is_file():
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="missing_screenshot_file",
                details=[f"Screenshot file listed in manifest does not exist: {file_rel}"],
            )
        raw_bytes = png_path.read_bytes()
        if not raw_bytes or not raw_bytes.startswith(_PNG_MAGIC):
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="invalid_screenshot_file",
                details=[f"Screenshot file '{file_rel}' is empty or not a valid PNG."],
            )
        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if not expected_sha or actual_sha != expected_sha:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="screenshot_sha256_mismatch",
                details=[f"SHA-256 mismatch for '{file_rel}': {actual_sha[:12]} != {expected_sha[:12]}."],
            )
        seen_viewports.add(vp)
        details.append(f"{file_rel} ({vp}, scale={scale}, axe=0, overflow=false)")

    if required_viewports:
        missing_vps = [vp for vp in required_viewports if vp not in seen_viewports]
        if missing_vps:
            return EvidenceValidationResult(
                verified=False,
                rejection_reason="missing_required_viewports",
                details=[f"Screenshot manifest missing required viewport(s): {missing_vps}."],
            )

    return EvidenceValidationResult(verified=True, details=details)


def write_screenshot_manifest(
    manifest_path: Path,
    *,
    build_id: str,
    commit_sha: str,
    renderer: str = "playwright-mockbridge",
    screenshots: list[dict[str, Any]],
) -> Path:
    """Write a machine-generated ``screenshot-manifest.json`` with SHA-256 digests."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "renderer": renderer,
        "build_id": build_id,
        "commit_sha": commit_sha,
        "screenshots": screenshots,
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


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
    """Persist per-job authoritative CI evidence records to SQLite."""
    ids: list[int] = []
    for job in observed_jobs:
        job_ok, job_reason = validation.job_results.get(
            job.name,
            (validation.verified and (job.status or "").lower() == "success", validation.rejection_reason),
        )
        # Overall validation failure (e.g. base_branch_drifted) or SHA mismatch invalidates the run as merge evidence.
        effective_verified = bool(validation.verified and job_ok and job.commit_sha == commit_sha)
        effective_reason = "" if effective_verified else (
            job_reason or ("stale_commit_sha" if job.commit_sha != commit_sha else validation.rejection_reason)
        )
        art_paths = [a.path if isinstance(a, CiArtifact) else str(a) for a in job.artifacts]
        rec_id = store.record_evidence(
            task_id=task_id,
            contract_sha256=contract_sha256,
            commit_sha=job.commit_sha or commit_sha,
            kind=kind,
            source=f"circleci:{job.name}",
            build_id=job.build_id,
            duration_seconds=job.duration_seconds,
            artifact_paths=art_paths,
            metadata={
                "job_name": job.name,
                "status": job.status,
                "compat_mode": job.compat_mode,
                "branch": job.branch,
                "expected_commit_sha": commit_sha,
            },
            verified=effective_verified,
            rejection_reason=effective_reason,
        )
        ids.append(rec_id)

    # If no observed jobs were returned (or a required job was missing), still record an audit row
    # so the rejection is durably persisted in SQLite.
    if not validation.verified and (not observed_jobs or validation.rejection_reason in ("missing_required_job", "base_branch_drifted", "missing_base_sha")):
        rec_id = store.record_evidence(
            task_id=task_id,
            contract_sha256=contract_sha256,
            commit_sha=commit_sha,
            kind=kind,
            source="circleci:summary",
            build_id="",
            duration_seconds=0.0,
            artifact_paths=[],
            metadata={"details": list(validation.details)},
            verified=False,
            rejection_reason=validation.rejection_reason,
        )
        ids.append(rec_id)
    return ids


def persist_screenshot_evidence(
    store: Store,
    *,
    task_id: str,
    contract_sha256: str,
    commit_sha: str,
    manifest_path: Path,
    validation: EvidenceValidationResult,
    build_id: str = "",
    renderer: str = "playwright-mockbridge",
) -> int:
    """Persist screenshot manifest verification outcome to SQLite ``evidence_records``."""
    return store.record_evidence(
        task_id=task_id,
        contract_sha256=contract_sha256,
        commit_sha=commit_sha,
        kind="screenshot",
        source=f"screenshot-manifest:{renderer}",
        build_id=build_id,
        duration_seconds=0.0,
        artifact_paths=[str(manifest_path)],
        metadata={"details": list(validation.details)},
        verified=validation.verified,
        rejection_reason="" if validation.verified else validation.rejection_reason,
    )


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
