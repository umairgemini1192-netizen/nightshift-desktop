"""Deliverable verification and diff guards: never trust exit codes alone (BLUEPRINT 1, 5d)."""

from __future__ import annotations

import fnmatch
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.engine.credentials import redact_known_secrets
from nightshift.engine.vcs import GitGhClient

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[0-9a-zA-Z]{36}"),
    re.compile(r"github_pat_[0-9a-zA-Z_]{82}"),
    re.compile(r"""(?i)(?:api[_-]?key|secret[_-]?key|auth[_-]?token)\s*[:=]\s*['"]([0-9a-zA-Z_\-]{16,})['"]"""),
]

TEST_SKIP_PATTERNS = [
    re.compile(r"^\+\s*@pytest\.mark\.skip", re.MULTILINE),
    re.compile(r"^\+\s*@pytest\.mark\.xfail", re.MULTILINE),
    re.compile(r"^\+\s*(?:it|test|describe)\.skip", re.MULTILINE),
    re.compile(r"^\+\s*(?:it|test|describe)\.only", re.MULTILINE),
]

CRITICAL_PATH_SIGNALS = [
    "auth",
    "login",
    "encryption",
    "key",
    "secret",
    "permission",
    "payment",
    "migration",
    "delete",
    "security",
    "AndroidManifest.xml",
    ".github/workflows",
    "scripts/check_",
]


@dataclass(frozen=True)
class GuardFinding:
    guard: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class RiskAssessment:
    level: str  # "low" or "critical"
    reasons: list[str] = field(default_factory=list)


def redact_secrets(text: str) -> str:
    """Redact known secret patterns and credentials from logs, events, and diffs."""
    redacted = redact_known_secrets(text)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def _shannon_entropy(data: str) -> float:
    if not data:
        return 0.0
    entropy = 0.0
    for x in set(data):
        p_x = float(data.count(x)) / len(data)
        if p_x > 0:
            entropy += -p_x * math.log2(p_x)
    return entropy


def verify_deliverable(
    vcs: GitGhClient,
    repo: str,
    branch: str,
    previous_sha: str | None = None,
    local_repo: Path | None = None,
) -> tuple[bool, str]:
    """Verify that a runner actually produced an open PR or pushed a new commit (NO-OP detection)."""
    pr = vcs.find_open_pr(repo, branch)
    if pr is not None:
        return True, f"Found open PR #{pr.get('number')} ({pr.get('url')})"

    if local_repo is not None:
        sha_after = vcs.remote_branch_sha(local_repo, branch)
        if sha_after and sha_after != previous_sha:
            return True, f"New commit {sha_after[:7]} pushed to branch {branch}"

    return False, f"NO-OP: runner exited 0 but no open PR exists and no new commit was pushed to {branch}."


def check_diff_guards(
    worktree_dir: Path,
    changed_files: list[str],
    diff_content: str,
    *,
    allow_ci_workflow_edits: bool = False,
    max_binary_size_bytes: int = 5 * 1024 * 1024,
    diff_entries: list[Any] | None = None,
    allowed_globs: list[str] | None = None,
    forbidden_globs: list[str] | None = None,
    extra_protected_paths: list[str] | None = None,
    test_file_snapshots: dict[str, tuple[str | None, str | None]] | None = None,
    case_fold: bool | None = None,
) -> list[GuardFinding]:
    """Run all BLUEPRINT section 5d & AGENT_PIPELINE Phase 8b hard diff guards.

    Returns a list of GuardFinding objects. If empty, all guards passed.
    """
    findings: list[GuardFinding] = []

    # 0. Reject NightShift internal runner prompt/handoff artifacts
    candidate_paths = list(changed_files or [])
    for entry in diff_entries or []:
        for p in (getattr(entry, "path", None), getattr(entry, "old_path", None)):
            if p and p not in candidate_paths:
                candidate_paths.append(p)
    for f in candidate_paths:
        norm = f.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        base = norm.rsplit("/", 1)[-1]
        if base == ".nightshift_prompt.md" or norm == ".nightshift_prompt.md":
            findings.append(
                GuardFinding(
                    guard="no_nightshift_artifacts",
                    message=f"Runner prompt file '{f}' must not be committed.",
                    path=f,
                )
            )

    # 1. Check for skipped or weakened tests
    for pattern in TEST_SKIP_PATTERNS:
        if pattern.search(diff_content):
            findings.append(
                GuardFinding(
                    guard="no_skipped_tests",
                    message="Diff introduces test skipping (e.g. @pytest.mark.skip, .only, or .skip).",
                )
            )
            break

    # 2. Check for weakened CI workflows or check scripts
    if not allow_ci_workflow_edits:
        for f in changed_files:
            norm = f.replace("\\", "/")
            if norm.startswith(".github/workflows/") or norm.startswith(".circleci/") or norm.startswith("scripts/check_"):
                findings.append(
                    GuardFinding(
                        guard="no_weakened_ci",
                        message=f"Unauthorized modification to CI workflow or gate script: {f}",
                        path=f,
                    )
                )

    # 3. Check for deleted test files (only when file is actually removed, not modified)
    lines = diff_content.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("--- a/"):
            candidate_path = line[6:].strip()
            next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
            prev_line = lines[idx - 1] if idx > 0 else ""
            is_deleted = next_line.startswith("+++ /dev/null") or prev_line.startswith("deleted file mode")
            if is_deleted and "test" in candidate_path.lower():
                findings.append(
                    GuardFinding(
                        guard="no_deleted_tests",
                        message=f"Test file deletion is forbidden: {candidate_path}",
                        path=candidate_path,
                    )
                )

    # 4. Check for secrets (patterns and high entropy)
    for pattern in SECRET_PATTERNS:
        match = pattern.search(diff_content)
        if match:
            findings.append(
                GuardFinding(
                    guard="no_secrets",
                    message="Secret or private credential pattern detected in diff.",
                )
            )
            break

    for line in diff_content.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:].strip()
            # Token/key assignment check
            if re.search(r"""(?i)(?:token|key|secret|password|cred)\s*[:=]\s*['"][^'"]{24,}['"]""", content):
                val_match = re.search(r"""['"]([^'"]{24,})['"]""", content)
                if val_match and _shannon_entropy(val_match.group(1)) > 4.2:
                    findings.append(
                        GuardFinding(
                            guard="no_secrets",
                            message="High entropy secret key assignment detected in diff.",
                        )
                    )
                    break

    # 5. Check for files outside worktree root (path traversal / symlink escape)
    for f in changed_files:
        p = worktree_dir / f
        try:
            resolved = p.resolve()
            if not str(resolved).startswith(str(worktree_dir.resolve())):
                findings.append(
                    GuardFinding(
                        guard="no_outside_files",
                        message=f"File resolves outside repository worktree root: {f}",
                        path=f,
                    )
                )
        except Exception:
            findings.append(
                GuardFinding(
                    guard="no_outside_files",
                    message=f"Invalid file path in diff: {f}",
                    path=f,
                )
            )

    # 6. Check for binaries over size limit (default 5 MB)
    for f in changed_files:
        p = worktree_dir / f
        if p.exists() and p.is_file():
            try:
                sz = p.stat().st_size
                if sz > max_binary_size_bytes:
                    findings.append(
                        GuardFinding(
                            guard="no_oversized_binaries",
                            message=f"File exceeds maximum allowed size ({sz / (1024*1024):.1f} MB > 5 MB): {f}",
                            path=f,
                        )
                    )
            except Exception:
                pass

    # 7. Phase 8b contract path guards (allowed_globs, forbidden_globs, rename-aware governance split)
    if diff_entries is not None or allowed_globs is not None or forbidden_globs is not None:
        from nightshift.engine.pipeline_contracts import DiffPathEntry, check_contract_path_guards

        entries = (
            list(diff_entries)
            if diff_entries is not None
            else [DiffPathEntry(status="M", path=f) for f in changed_files]
        )
        path_findings, _ = check_contract_path_guards(
            entries,
            allowed_globs=list(allowed_globs or []),
            forbidden_globs=forbidden_globs,
            extra_protected_paths=extra_protected_paths,
            diff_content=diff_content,
            case_fold=case_fold,
        )
        findings.extend(path_findings)

    # 8. Phase 8b blocking assertion-relaxation detector (AST Python + regex JS/TS)
    if test_file_snapshots:
        from nightshift.engine.pipeline_contracts import detect_assertion_relaxation_for_files

        findings.extend(detect_assertion_relaxation_for_files(test_file_snapshots, case_fold=case_fold))

    return findings


def assess_diff_risk(
    changed_files: list[str],
    diff_content: str,
    critical_paths: list[str] | None = None,
) -> RiskAssessment:
    """Classify diff risk as 'low' or 'critical' per BLUEPRINT section 5d."""
    reasons: list[str] = []
    user_patterns = critical_paths or []

    for f in changed_files:
        norm = f.replace("\\", "/")
        # User defined critical paths
        for pat in user_patterns:
            if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(f, pat):
                reasons.append(f"Matches critical path pattern '{pat}': {f}")
                break

        # Built-in critical signals
        for sig in CRITICAL_PATH_SIGNALS:
            if sig.lower() in norm.lower():
                reasons.append(f"Touches critical system signal '{sig}': {f}")
                break

    # Check diff content for dependency additions or sensitive keywords
    for line in diff_content.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            low = line[1:].lower()
            if "permission" in low or "uses-permission" in low:
                reasons.append("Adds system permissions in diff.")
                break
            if "dependencies" in low and ("implementation" in low or "require" in low):
                reasons.append("Modifies dependencies in build manifest.")
                break

    level = "critical" if reasons else "low"
    return RiskAssessment(level=level, reasons=reasons)
