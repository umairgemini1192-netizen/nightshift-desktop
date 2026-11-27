"""Risk classification, provider-independent review, validated citations, and release governance.

Implements Governance Amendments C, G, H and docs/AGENT_PIPELINE.md sections 2, 6.1, 9.2, 9.3.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from nightshift.engine.pipeline_contracts import (
    RISK_CLASS_ORDER,
    DiffPathEntry,
    is_governance_path,
    normalize_repo_path,
)
from nightshift.store import Store

# Provider-independence mapping (AGENT_PIPELINE §2)
ENGINE_PROVIDER_MAP: dict[str, str] = {
    "agy": "google",
    "gemini": "google",
    "codex": "openai",
    "claude": "anthropic",
    "qwen": "alibaba",
    "deepseek": "deepseek",
    "kimi": "moonshot",
    "glm": "zhipu",
    "minimax": "minimax",
    "script": "local",
}

HIGH_RISK_PATH_KEYWORDS: tuple[str, ...] = (
    "auth",
    "login",
    "credential",
    "dpapi",
    "secret",
    "encrypt",
    "payment",
    "billing",
    "money",
    "finance",
    "migration",
    "store.py",
    "permission",
    "androidmanifest.xml",
    "security",
)

DOCS_EXTENSIONS: tuple[str, ...] = (".md", ".txt", ".rst")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def engine_provider(engine_id: str) -> str:
    """Resolve the underlying model provider for an engine/CLI identifier."""
    base = engine_id.strip().lower().split("-")[0].split(":")[0]
    return ENGINE_PROVIDER_MAP.get(base, base or "unknown")


def compute_risk_score(factors: dict[str, Any]) -> float:
    """Compute the 5-factor weighted risk score (1.0 - 5.0) per AGENT_PIPELINE §6.1."""
    impact = float(factors.get("impact", 3))
    data_sensitivity = float(factors.get("data_sensitivity", 2))
    reversibility = float(factors.get("reversibility", 3))
    test_coverage = float(factors.get("test_coverage", 3))
    uncertainty = float(factors.get("uncertainty", 2))
    score = (
        0.30 * impact
        + 0.25 * data_sensitivity
        + 0.20 * (6.0 - reversibility)
        + 0.15 * (6.0 - test_coverage)
        + 0.10 * uncertainty
    )
    return round(score, 3)


def _max_risk_class(a: str, b: str) -> str:
    return a if RISK_CLASS_ORDER.get(a, 2) >= RISK_CLASS_ORDER.get(b, 2) else b


def classify_task_risk(
    *,
    entries: list[DiffPathEntry],
    diff_content: str = "",
    risk_factors: dict[str, Any] | None = None,
    planned_risk_class: str = "docs_format",
    extra_protected_paths: list[str] | None = None,
    case_fold: bool | None = None,
) -> tuple[str, float, list[str]]:
    """Classify task risk mechanically from paths, diff content, and risk factors.

    Hard path/content overrides ALWAYS escalate upward, never downward.
    Returns ``(risk_class, risk_score, reasons)``.
    """
    score = compute_risk_score(risk_factors or {})
    reasons: list[str] = []

    if score >= 3.8:
        inferred = "high_risk"
        reasons.append(f"Risk factor score {score:.2f} >= 3.80")
    elif score >= 2.5:
        inferred = "business_logic"
    elif score >= 1.8:
        inferred = "ui_cosmetic"
    else:
        inferred = "docs_format"

    risk_class = _max_risk_class(planned_risk_class, inferred)

    all_paths = [p for e in entries for p in e.all_paths()]
    if not all_paths:
        return risk_class, score, reasons

    all_docs = True
    all_ui_modules = True
    for raw_path in all_paths:
        norm = normalize_repo_path(raw_path, case_fold=case_fold)
        if is_governance_path(
            raw_path,
            extra_protected_paths=extra_protected_paths,
            diff_content=diff_content,
            case_fold=case_fold,
        ):
            risk_class = "governance"
            reasons.append(f"Protected governance path touched: {raw_path}")
            continue

        if not norm.endswith(DOCS_EXTENSIONS):
            all_docs = False
        if not (norm.startswith("frontend/src/modules/") and norm.endswith((".tsx", ".ts", ".css"))):
            all_ui_modules = False

        if any(kw in norm.lower() for kw in HIGH_RISK_PATH_KEYWORDS):
            risk_class = _max_risk_class(risk_class, "high_risk")
            reasons.append(f"High-risk path signal in '{raw_path}'")

    if risk_class != "governance":
        if not all_docs and not all_ui_modules:
            risk_class = _max_risk_class(risk_class, "business_logic")
        elif all_ui_modules and not all_docs:
            risk_class = _max_risk_class(risk_class, "ui_cosmetic")

    # Content-level high-risk signals
    lowered = diff_content.lower()
    if any(tok in lowered for tok in ("create table", "alter table", "drop table", "dpapi", "uses-permission")):
        risk_class = _max_risk_class(risk_class, "high_risk")
        reasons.append("Diff content modifies schema, credentials, or permissions")

    return risk_class, score, reasons


@dataclass(frozen=True)
class ReviewerCandidate:
    engine_id: str
    model: str
    tier: str  # "economy" | "standard" | "gold"
    available: bool = True
    benchmark_graduated: bool = False


def select_independent_reviewer(
    *,
    candidates: list[ReviewerCandidate],
    contributing_providers: list[str] | set[str],
    risk_class: str,
) -> tuple[ReviewerCandidate | None, str]:
    """Select an independent cross-provider reviewer excluding ALL contributing providers.

    Never falls back to self-review; returns ``(None, reason)`` if no independent provider exists.
    """
    excluded = {p.strip().lower() for p in contributing_providers if p}
    strong_only = risk_class in ("high_risk", "governance")

    for cand in candidates:
        if not cand.available:
            continue
        prov = engine_provider(cand.engine_id)
        if prov in excluded:
            continue
        if strong_only and cand.tier not in ("standard", "gold"):
            continue
        if cand.tier == "economy" and not cand.benchmark_graduated:
            continue
        return cand, f"Selected independent reviewer {cand.engine_id} (provider={prov}, excluded={sorted(excluded)})"

    return (
        None,
        f"no_independent_reviewer: no eligible reviewer outside contributing_providers={sorted(excluded)} for risk_class={risk_class}",
    )


def build_independent_review_prompt(
    *,
    contract_canonical_json: str,
    raw_git_diff: str,
) -> str:
    """Build the strictly isolated reviewer prompt (Amendment G: raw diff + contract ONLY).

    Never includes the coder's PR title, PR body, commit messages, or self-justification.
    """
    return (
        "You are an independent cross-provider code reviewer.\n"
        "Review ONLY the frozen Requirement Contract and raw git diff below.\n"
        "You have read-only access to the repository at HEAD to check callers/dependents.\n"
        "Return strict JSON matching this schema:\n"
        '{"verdict": "APPROVE" | "CHANGES", "findings": ['
        '{"priority": "P1" | "P2", "kind": "defect" | "omission", '
        '"file": "path/to/file.py", "line": 42, "ac_id": "ac-1", "message": "..."}]}\n'
        "Every finding MUST cite either a real file:line in the repository at HEAD, or an omission with ac_id + target file.\n\n"
        "FROZEN_CONTRACT_JSON:\n"
        f"{contract_canonical_json}\n\n"
        "RAW_GIT_DIFF:\n"
        f"```diff\n{raw_git_diff}\n```\n"
    )


@dataclass(frozen=True)
class ValidatedFinding:
    priority: str  # "P1" | "P2"
    kind: str  # "defect" | "omission"
    file: str
    line: int | None
    ac_id: str | None
    message: str


@dataclass(frozen=True)
class ReviewValidationResult:
    valid: bool
    verdict: str  # "APPROVE" | "CHANGES" | "INVALID"
    findings: list[ValidatedFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate_review_response(
    raw_output: str,
    *,
    repo_root: Path,
    contract_ac_ids: set[str] | None = None,
) -> ReviewValidationResult:
    """Parse and validate reviewer JSON output, enforcing real repo file:line or omission citations."""
    text = raw_output.strip()
    # Extract JSON block if wrapped in markdown fences
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    try:
        data = json.loads(text)
    except Exception as exc:
        return ReviewValidationResult(False, "INVALID", errors=[f"Reviewer output is not valid JSON: {exc}"])

    verdict = str(data.get("verdict") or "").upper()
    if verdict not in ("APPROVE", "CHANGES"):
        return ReviewValidationResult(False, "INVALID", errors=[f"Invalid verdict '{verdict}'; expected APPROVE or CHANGES."])

    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        return ReviewValidationResult(False, "INVALID", errors=["'findings' must be a JSON list."])

    validated: list[ValidatedFinding] = []
    errors: list[str] = []
    ac_ids = contract_ac_ids or set()

    for idx, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            errors.append(f"Finding #{idx} is not an object.")
            continue
        priority = str(item.get("priority") or "").upper()
        kind = str(item.get("kind") or "defect").lower()
        file_rel = str(item.get("file") or "").strip()
        line_val = item.get("line")
        ac_id = str(item.get("ac_id") or "").strip() or None
        message = str(item.get("message") or "").strip()

        if priority not in ("P1", "P2"):
            errors.append(f"Finding #{idx} has invalid priority '{priority}'.")
            continue
        if not message:
            errors.append(f"Finding #{idx} has empty message.")
            continue
        if not file_rel:
            errors.append(f"Finding #{idx} is missing a file citation.")
            continue

        target_path = (repo_root / file_rel).resolve()
        try:
            target_path.relative_to(repo_root.resolve())
        except ValueError:
            errors.append(f"Finding #{idx} cites path outside repo: '{file_rel}'.")
            continue

        if kind == "omission":
            if not ac_id or (ac_ids and ac_id not in ac_ids):
                errors.append(f"Omission finding #{idx} must cite a valid acceptance_criteria id (got '{ac_id}').")
                continue
            # Target module/file or its parent directory must exist
            if not target_path.exists() and not target_path.parent.exists():
                errors.append(f"Omission finding #{idx} target path does not exist: '{file_rel}'.")
                continue
            line_int = int(line_val) if line_val is not None else None
            validated.append(ValidatedFinding(priority, "omission", file_rel, line_int, ac_id, message))
        else:
            if not target_path.is_file():
                errors.append(f"Finding #{idx} cites non-existent file '{file_rel}'.")
                continue
            if line_val is None or not isinstance(line_val, int) or line_val < 1:
                errors.append(f"Finding #{idx} must cite a positive integer line number in '{file_rel}'.")
                continue
            line_count = len(target_path.read_text(encoding="utf-8", errors="replace").splitlines())
            if line_val > max(1, line_count):
                errors.append(f"Finding #{idx} cites line {line_val} beyond EOF ({line_count}) in '{file_rel}'.")
                continue
            validated.append(ValidatedFinding(priority, "defect", file_rel, line_val, ac_id, message))

    if errors:
        return ReviewValidationResult(False, "INVALID", findings=validated, errors=errors)

    # Any P1 finding forces CHANGES even if model wrote APPROVE
    if any(f.priority == "P1" for f in validated):
        verdict = "CHANGES"

    return ReviewValidationResult(True, verdict, findings=validated)


@dataclass(frozen=True)
class SmokeTestOutcome:
    status: str  # "passed" | "failed" | "unavailable"
    detail: str


class ReleaseGovernance:
    """Post-release launch/smoke verification (fail-closed) and one-click rollback (Amendment H)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def verify_post_release_smoke(
        self,
        *,
        tag: str,
        task_id: str | None,
        repo: str,
        merge_sha: str,
        artifact_sha256: str,
        smoke_command: str | None,
        smoke_runner: Callable[[str], tuple[int, str]] | None = None,
        previous_good_tag: str | None = None,
    ) -> SmokeTestOutcome:
        """Execute mandatory configured launch/smoke test; fail closed if unavailable or failing."""
        if not smoke_command or not smoke_command.strip() or smoke_runner is None:
            detail = "No launch/smoke procedure configured or runner unavailable; failing closed (BLOCKED)."
            self.store.upsert_release_verification(
                tag=tag,
                task_id=task_id,
                repo=repo,
                merge_sha=merge_sha,
                previous_good_tag=previous_good_tag,
                artifact_sha256=artifact_sha256,
                smoke_command=smoke_command or "",
                smoke_status="failed",
                smoke_detail=detail,
            )
            return SmokeTestOutcome("unavailable", detail)

        rc, output = smoke_runner(smoke_command)
        if rc == 0:
            now = _utc_now()
            self.store.upsert_release_verification(
                tag=tag,
                task_id=task_id,
                repo=repo,
                merge_sha=merge_sha,
                previous_good_tag=previous_good_tag,
                artifact_sha256=artifact_sha256,
                smoke_command=smoke_command,
                smoke_status="passed",
                smoke_detail=output or "Smoke test passed",
                verified_at=now,
            )
            return SmokeTestOutcome("passed", output or "Smoke test passed")

        detail = f"Smoke test exited with code {rc}: {output}"
        self.store.upsert_release_verification(
            tag=tag,
            task_id=task_id,
            repo=repo,
            merge_sha=merge_sha,
            previous_good_tag=previous_good_tag,
            artifact_sha256=artifact_sha256,
            smoke_command=smoke_command,
            smoke_status="failed",
            smoke_detail=detail,
        )
        return SmokeTestOutcome("failed", detail)

    def rollback_release(
        self,
        *,
        faulty_tag: str,
        archive_dir: Path,
        output_dir: Path,
    ) -> dict[str, Any]:
        """Restore previous_good_tag's artifact + SHA256SUMS into output_dir and mark faulty_tag rolled_back."""
        rec = self.store.get_release_verification(faulty_tag)
        if rec is None:
            raise KeyError(f"Unknown release tag: {faulty_tag}")
        prev_tag = rec.get("previous_good_tag")
        if not prev_tag:
            raise ValueError(f"Release {faulty_tag} has no recorded previous_good_tag to roll back to.")

        prev_dir = archive_dir / str(prev_tag)
        if not prev_dir.exists():
            raise FileNotFoundError(f"Archived artifacts for {prev_tag} not found at {prev_dir}")

        output_dir.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        for item in prev_dir.iterdir():
            if item.is_file():
                dest = output_dir / item.name
                shutil.copy2(item, dest)
                restored.append(str(dest))

        now = _utc_now()
        updated = self.store.upsert_release_verification(
            tag=faulty_tag,
            task_id=rec.get("task_id"),
            repo=str(rec["repo"]),
            merge_sha=str(rec["merge_sha"]),
            previous_good_tag=str(prev_tag),
            artifact_sha256=str(rec["artifact_sha256"]),
            smoke_command=str(rec["smoke_command"]),
            smoke_status="rolled_back",
            smoke_detail=f"Superseded and rolled back to {prev_tag} at {now}",
            rolled_back_at=now,
        )
        return {
            "faultyTag": faulty_tag,
            "restoredTag": prev_tag,
            "restoredFiles": restored,
            "verification": updated,
        }
