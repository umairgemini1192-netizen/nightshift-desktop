"""Risk classifier (Amendment C) and provider-independent reviewer (Amendment G).

Implements docs/AGENT_PIPELINE.md sections 2, 6.1, and 9.2 (Phase 8e).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nightshift.engine.pipeline_contracts import (
    DiffPathEntry,
    RISK_CLASS_ORDER,
    _glob_matches,
    is_governance_path,
    is_test_file_path,
    normalize_repo_path,
    provider_for_engine,
)
from nightshift.runners.base import Runner

# Engine tier classification (BLUEPRINT §5h, AGENT_PIPELINE §§2, 6.1)
ENGINE_TIER_MAP: dict[str, str] = {
    "claude": "gold",
    "codex": "standard",
    "gpt": "standard",
    "agy": "standard",
    "antigravity": "standard",
    "gemini": "standard",
    "qwen": "economy",
    "deepseek": "economy",
    "kimi": "economy",
    "glm": "economy",
    "minimax": "economy",
}

STRONG_TIERS: frozenset[str] = frozenset({"standard", "gold"})

# Default reviewer preference order (Standard/Gold first, then Economy if graduated)
DEFAULT_REVIEWER_ORDER: tuple[str, ...] = (
    "codex",
    "claude",
    "gemini",
    "agy",
    "qwen",
    "deepseek",
    "kimi",
    "glm",
    "minimax",
)

# High-risk path & content signals (AGENT_PIPELINE §6.1)
HIGH_RISK_PATH_SIGNALS: tuple[str, ...] = (
    "auth",
    "login",
    "dpapi",
    "credential",
    "secret",
    "encrypt",
    "decrypt",
    "cipher",
    "keystore",
    "security",
    "payment",
    "billing",
    "invoice",
    "finance",
    "money",
    "stripe",
    "payout",
    "permission",
    "androidmanifest.xml",
    "migration",
)

HIGH_RISK_DIFF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("db_schema_migration", re.compile(r"\b(?:ALTER\s+TABLE|CREATE\s+TABLE|DROP\s+TABLE|_migration_\d+)\b", re.IGNORECASE)),
    ("system_permissions", re.compile(r"\b(?:uses-permission|android\.permission\.|RequestPermission)\b", re.IGNORECASE)),
    ("dpapi_or_crypto", re.compile(r"\b(?:CryptProtectData|CryptUnprotectData|Fernet|AESGCM|PBKDF2|private_key)\b")),
    ("external_file_write", re.compile(r"(?:C:\\Windows|/etc/|HKLM|HKEY_LOCAL_MACHINE)", re.IGNORECASE)),
)

DOCS_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".rst"})
UI_COSMETIC_GLOBS: tuple[str, ...] = (
    "frontend/src/modules/**",
    "frontend/src/design/**",
    "frontend/src/app/**",
    "frontend/src/**/*.css",
    "frontend/src/**/*.svg",
)


def tier_for_engine(engine_name: str, *, explicit_tier: str | None = None) -> str:
    """Resolve the canonical cost/capability tier (economy | standard | gold) for an engine."""
    if explicit_tier and explicit_tier.strip().lower() in ("economy", "standard", "gold"):
        return explicit_tier.strip().lower()
    raw = (engine_name or "").strip().lower()
    if not raw:
        return "economy"
    if raw in ENGINE_TIER_MAP:
        return ENGINE_TIER_MAP[raw]
    for key, tier in ENGINE_TIER_MAP.items():
        if raw.startswith(f"{key}-") or raw.startswith(f"{key}_") or f"-{key}" in raw:
            return tier
    return "economy"


@dataclass(frozen=True)
class RiskPolicy:
    """Verification, review, and merge policy for a single risk class (AGENT_PIPELINE §6.1)."""

    risk_class: str
    requires_independent_review: bool
    requires_strong_reviewer: bool
    requires_owner_approval: bool
    requires_screenshots: bool
    auto_merge_eligible: bool


RISK_CLASS_POLICIES: dict[str, RiskPolicy] = {
    "docs_format": RiskPolicy(
        risk_class="docs_format",
        requires_independent_review=False,
        requires_strong_reviewer=False,
        requires_owner_approval=False,
        requires_screenshots=False,
        auto_merge_eligible=True,
    ),
    "ui_cosmetic": RiskPolicy(
        risk_class="ui_cosmetic",
        requires_independent_review=False,
        requires_strong_reviewer=False,
        requires_owner_approval=False,
        requires_screenshots=True,
        auto_merge_eligible=True,
    ),
    "business_logic": RiskPolicy(
        risk_class="business_logic",
        requires_independent_review=True,
        requires_strong_reviewer=False,
        requires_owner_approval=False,
        requires_screenshots=False,
        auto_merge_eligible=True,
    ),
    "high_risk": RiskPolicy(
        risk_class="high_risk",
        requires_independent_review=True,
        requires_strong_reviewer=True,
        requires_owner_approval=True,
        requires_screenshots=False,
        auto_merge_eligible=False,
    ),
    "governance": RiskPolicy(
        risk_class="governance",
        requires_independent_review=True,
        requires_strong_reviewer=True,
        requires_owner_approval=True,
        requires_screenshots=False,
        auto_merge_eligible=False,
    ),
}


def policy_for_risk_class(risk_class: str) -> RiskPolicy:
    """Return the RiskPolicy for ``risk_class`` (defaults fail-safe to ``business_logic``)."""
    return RISK_CLASS_POLICIES.get(str(risk_class or "").strip().lower(), RISK_CLASS_POLICIES["business_logic"])


def compute_risk_score(risk_factors: dict[str, Any] | None) -> float:
    """Compute weighted 1.0–5.0 risk score per AGENT_PIPELINE §6.1.

    ``risk_score = 0.30 * impact + 0.25 * data_sensitivity + 0.20 * (6 - reversibility)
                   + 0.15 * (6 - test_coverage) + 0.10 * uncertainty``
    """
    factors = risk_factors or {}

    def _clamp(name: str, default: float) -> float:
        raw = factors.get(name, default)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = default
        return max(1.0, min(5.0, val))

    impact = _clamp("impact", 2.0)
    data_sensitivity = _clamp("data_sensitivity", 1.0)
    reversibility = _clamp("reversibility", 4.0)
    test_coverage = _clamp("test_coverage", 4.0)
    uncertainty = _clamp("uncertainty", 2.0)

    score = (
        0.30 * impact
        + 0.25 * data_sensitivity
        + 0.20 * (6.0 - reversibility)
        + 0.15 * (6.0 - test_coverage)
        + 0.10 * uncertainty
    )
    return round(score, 3)


@dataclass(frozen=True)
class RiskClassification:
    """Mechanical risk classification result (Amendment C)."""

    risk_class: str
    score: float
    reasons: list[str] = field(default_factory=list)
    escalated_from: str | None = None

    @property
    def policy(self) -> RiskPolicy:
        return policy_for_risk_class(self.risk_class)

    @property
    def requires_independent_review(self) -> bool:
        return self.policy.requires_independent_review

    @property
    def requires_strong_reviewer(self) -> bool:
        return self.policy.requires_strong_reviewer

    @property
    def requires_owner_approval(self) -> bool:
        return self.policy.requires_owner_approval

    @property
    def requires_screenshots(self) -> bool:
        return self.policy.requires_screenshots

    @property
    def auto_merge_eligible(self) -> bool:
        return self.policy.auto_merge_eligible


def _escalate_risk(current: str, candidate: str) -> str:
    """Return the higher-risk class between ``current`` and ``candidate`` (never downgrades)."""
    cur_rank = RISK_CLASS_ORDER.get(current, RISK_CLASS_ORDER["business_logic"])
    cand_rank = RISK_CLASS_ORDER.get(candidate, RISK_CLASS_ORDER["business_logic"])
    return candidate if cand_rank > cur_rank else current


def _is_docs_path(path: str, *, case_fold: bool | None = None) -> bool:
    norm = normalize_repo_path(path, case_fold=case_fold)
    low = norm.lower()
    if low.endswith(tuple(DOCS_EXTENSIONS)):
        return True
    return False


def _is_ui_cosmetic_path(path: str, *, case_fold: bool | None = None) -> bool:
    norm = normalize_repo_path(path, case_fold=case_fold)
    return any(_glob_matches(norm, pat, case_fold=case_fold) for pat in UI_COSMETIC_GLOBS)


def classify_task_risk(
    entries: list[DiffPathEntry],
    diff_content: str = "",
    *,
    base_risk_class: str | None = None,
    risk_factors: dict[str, Any] | None = None,
    critical_paths: list[str] | None = None,
    extra_protected_paths: list[str] | None = None,
    case_fold: bool | None = None,
) -> RiskClassification:
    """Mechanically classify a task's diff into one of the 5 risk classes (AGENT_PIPELINE §6.1).

    Hard path and content overrides ALWAYS escalate upward and NEVER downgrade below
    ``base_risk_class``. Evaluates both old and new paths for renames, copies, and deletions.
    """
    reasons: list[str] = []
    score = compute_risk_score(risk_factors)
    user_critical = list(critical_paths or [])

    all_paths: list[str] = []
    for entry in entries:
        for p in entry.all_paths():
            if p and p not in all_paths:
                all_paths.append(p)

    # 1. Start from path-derived baseline when paths are present
    detected = "docs_format"
    if not all_paths:
        detected = str(base_risk_class or "business_logic").strip().lower()
        if detected not in RISK_CLASS_ORDER:
            detected = "business_logic"
    else:
        non_docs = False
        non_ui = False
        for raw_path in all_paths:
            norm = normalize_repo_path(raw_path, case_fold=case_fold)
            if is_governance_path(
                raw_path,
                extra_protected_paths=extra_protected_paths,
                diff_content=diff_content,
                case_fold=case_fold,
            ):
                detected = _escalate_risk(detected, "governance")
                reasons.append(f"Protected governance path touched: {raw_path}")
                continue

            # Project-configured critical_paths -> high_risk
            matched_critical = False
            for pat in user_critical:
                if _glob_matches(raw_path, pat, case_fold=case_fold):
                    detected = _escalate_risk(detected, "high_risk")
                    reasons.append(f"Matches critical path '{pat}': {raw_path}")
                    matched_critical = True
                    break

            if not matched_critical:
                low_norm = norm.lower()
                for sig in HIGH_RISK_PATH_SIGNALS:
                    if sig in low_norm:
                        detected = _escalate_risk(detected, "high_risk")
                        reasons.append(f"Matches high-risk path signal '{sig}': {raw_path}")
                        break

            if not _is_docs_path(raw_path, case_fold=case_fold):
                non_docs = True
                if not _is_ui_cosmetic_path(raw_path, case_fold=case_fold):
                    non_ui = True

        if non_docs:
            if non_ui:
                detected = _escalate_risk(detected, "business_logic")
            else:
                detected = _escalate_risk(detected, "ui_cosmetic")

    # 2. Content-level high-risk signals on added lines
    added_lines = [
        line[1:]
        for line in diff_content.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    added_text = "\n".join(added_lines)
    if added_text:
        for label, pattern in HIGH_RISK_DIFF_PATTERNS:
            if pattern.search(added_text):
                detected = _escalate_risk(detected, "high_risk")
                reasons.append(f"Diff introduces high-risk pattern '{label}'")

    # 3. Numeric risk_score escalation (when risk_factors provided)
    if risk_factors:
        if score >= 3.8:
            detected = _escalate_risk(detected, "high_risk")
            reasons.append(f"Weighted risk score {score:.2f} >= 3.80")
        elif score >= 2.2:
            detected = _escalate_risk(detected, "business_logic")

    # 4. Never downgrade below base_risk_class
    norm_base = str(base_risk_class or "").strip().lower()
    final_class = detected
    escalated_from: str | None = None
    if norm_base in RISK_CLASS_ORDER:
        final_class = _escalate_risk(norm_base, detected)
        if RISK_CLASS_ORDER[final_class] > RISK_CLASS_ORDER[norm_base]:
            escalated_from = norm_base

    return RiskClassification(
        risk_class=final_class,
        score=score,
        reasons=reasons,
        escalated_from=escalated_from,
    )


# ---------------------------------------------------------------------------
# Priority-packed raw diff & strict input-isolated reviewer prompt (Amendment G)
# ---------------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")


def _split_diff_sections(raw_diff: str) -> list[tuple[str, str]]:
    """Split a unified ``git diff`` into ``(path, section_text)`` pairs."""
    if not raw_diff.strip():
        return []
    sections: list[tuple[str, list[str]]] = []
    current_path = "unknown"
    current_lines: list[str] = []
    for line in raw_diff.splitlines():
        m = _DIFF_HEADER_RE.match(line)
        if m:
            if current_lines:
                sections.append((current_path, current_lines))
            current_path = m.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_path, current_lines))
    return [(p, "\n".join(lines)) for p, lines in sections]


def _diff_section_priority(path: str) -> int:
    """Priority order per BLUEPRINT §5d / AGENT_PIPELINE §9.2: app code (0) -> resources (1) -> tests (2)."""
    if is_test_file_path(path):
        return 2
    low = path.lower()
    if low.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java", ".go", ".rs")):
        return 0
    return 1


def _split_section_hunks(section_text: str) -> tuple[str, list[str]]:
    """Separate a file diff section into its header and individual ``@@`` hunks."""
    lines = section_text.splitlines()
    header: list[str] = []
    hunks: list[list[str]] = []
    current_hunk: list[str] | None = None
    for line in lines:
        if line.startswith("@@ "):
            if current_hunk is not None:
                hunks.append(current_hunk)
            current_hunk = [line]
        elif current_hunk is not None:
            current_hunk.append(line)
        else:
            header.append(line)
    if current_hunk is not None:
        hunks.append(current_hunk)
    return "\n".join(header), ["\n".join(h) for h in hunks]


def pack_raw_diff_for_review(raw_diff: str, *, max_chars: int = 60000) -> str:
    """Pack raw ``git diff`` by priority (app -> resources -> tests) using whole hunks only.

    Never truncates mid-hunk; every file receives at least its first hunk when space permits.
    """
    if len(raw_diff) <= max_chars:
        return raw_diff

    sections = _split_diff_sections(raw_diff)
    if not sections:
        return raw_diff[:max_chars]

    ordered = sorted(sections, key=lambda item: _diff_section_priority(item[0]))
    packed_parts: list[str] = []
    used = 0

    # Pass 1: include each file's header + first complete hunk (if it fits)
    remaining_hunks: list[tuple[str, list[str]]] = []
    for path, sec_text in ordered:
        header, hunks = _split_section_hunks(sec_text)
        if not hunks:
            if used + len(sec_text) + 2 <= max_chars:
                packed_parts.append(sec_text)
                used += len(sec_text) + 2
            continue
        first_chunk = f"{header}\n{hunks[0]}"
        if used + len(first_chunk) + 2 <= max_chars:
            packed_parts.append(first_chunk)
            used += len(first_chunk) + 2
            if len(hunks) > 1:
                remaining_hunks.append((path, hunks[1:]))
        else:
            remaining_hunks.append((path, hunks))

    # Pass 2: append additional whole hunks in priority order while budget remains
    for path, hunks in remaining_hunks:
        for hunk in hunks:
            block = f"# Additional hunk for {path}\n{hunk}"
            if used + len(block) + 2 <= max_chars:
                packed_parts.append(block)
                used += len(block) + 2
            else:
                break

    return "\n\n".join(packed_parts)


REVIEW_OUTPUT_SCHEMA_DOC = """{
  "verdict": "APPROVE" | "CHANGES",
  "summary": "One-sentence plain-English summary of the review.",
  "findings": [
    {
      "priority": "P1" | "P2",
      "kind": "defect" | "omission",
      "file": "relative/path/to/file.py",
      "line": 42,
      "acceptance_criteria_id": "ac-1 (required ONLY when kind is omission)",
      "message": "Clear description of the bug, regression, security flaw, or missing requirement."
    }
  ]
}"""


def build_isolated_review_prompt(
    *,
    contract_canonical_json: str,
    raw_diff: str,
    head_sha: str = "",
    max_diff_chars: int = 60000,
    retry_validation_errors: list[str] | None = None,
) -> str:
    """Build a strictly input-isolated independent reviewer prompt (AGENT_PIPELINE §9.2).

    Contains ONLY:
    1. The frozen ``task_contract`` canonical JSON,
    2. The raw ``git diff`` (priority-packed, whole hunks),
    3. Read-only worktree inspection instructions, and
    4. The required JSON output schema.

    NEVER includes PR title, PR body, commit messages, or coder self-justification.
    """
    packed_diff = pack_raw_diff_for_review(raw_diff, max_chars=max_diff_chars)
    sha_note = f" at commit {head_sha}" if head_sha else ""
    retry_block = ""
    if retry_validation_errors:
        err_lines = "\n".join(f"- {e}" for e in retry_validation_errors)
        retry_block = (
            "\nPREVIOUS OUTPUT REJECTED FOR SCHEMA/CITATION ERRORS:\n"
            f"{err_lines}\n"
            "Fix every error above and return ONLY valid JSON matching the schema.\n"
        )

    return (
        f"You are an independent cross-provider code reviewer inspecting a repository checkout{sha_note}.\n"
        "You have READ-ONLY access to the checked-out worktree to verify callers and context.\n"
        "Do NOT modify files, run git mutations, or rely on any PR description or commit narrative.\n\n"
        "FROZEN TASK CONTRACT (JSON):\n"
        f"```json\n{contract_canonical_json}\n```\n\n"
        "RAW GIT DIFF:\n"
        f"```diff\n{packed_diff}\n```\n\n"
        "REVIEW RULES:\n"
        "- P1 = blocking defect (bug, crash, data loss, security/privacy flaw, broken accessibility, "
        "trust-boundary bypass, or missing acceptance criterion).\n"
        "- P2 = non-blocking suggestion or minor improvement.\n"
        "- Every finding MUST include either:\n"
        "  1. kind='defect' with an exact 'file' and 1-based 'line' that exists in the checked-out repository, OR\n"
        "  2. kind='omission' with 'acceptance_criteria_id' from the frozen contract plus the target 'file' "
        "(and optional 'line' if the file exists).\n"
        "- If there are any P1 findings, verdict MUST be 'CHANGES'. If zero P1 findings, verdict MUST be 'APPROVE'.\n"
        f"{retry_block}\n"
        "REQUIRED JSON OUTPUT SCHEMA (respond with JSON only):\n"
        f"```json\n{REVIEW_OUTPUT_SCHEMA_DOC}\n```\n"
    )


# ---------------------------------------------------------------------------
# Structured review output parsing & citation validation (Amendment G)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidatedReviewFinding:
    """Single citation-validated finding from an independent reviewer."""

    priority: str  # "P1" | "P2"
    kind: str  # "defect" | "omission"
    file: str
    line: int | None
    message: str
    acceptance_criteria_id: str | None = None


@dataclass(frozen=True)
class ValidatedReviewResult:
    """Validated structured output of Stage 6 (REVIEW)."""

    valid: bool
    verdict: str  # "APPROVE" | "CHANGES" | "INVALID"
    summary: str = ""
    findings: list[ValidatedReviewFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reviewer_engine: str = ""
    reviewer_provider: str = ""
    reviewer_tier: str = ""

    @property
    def p1_findings(self) -> list[ValidatedReviewFinding]:
        return [f for f in self.findings if f.priority == "P1"]

    @property
    def p2_findings(self) -> list[ValidatedReviewFinding]:
        return [f for f in self.findings if f.priority == "P2"]


def _extract_json_object(raw_output: str) -> dict[str, Any] | None:
    """Extract a JSON object from raw reviewer stdout (handles fenced ```json blocks)."""
    text = (raw_output or "").strip()
    if not text:
        return None
    # Try direct parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # Try fenced json block
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    # Fallback: first '{' to last '}'
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _count_file_lines_safe(worktree_dir: Path, rel_path: str) -> tuple[bool, int, str]:
    """Safely verify ``rel_path`` exists inside ``worktree_dir`` and return ``(exists, line_count, err)``."""
    norm = rel_path.strip().replace("\\", "/")
    if not norm or norm.startswith("/") or ".." in norm.split("/"):
        return False, 0, f"Invalid or unsafe relative path '{rel_path}'."
    try:
        root = worktree_dir.resolve()
        target = (worktree_dir / norm).resolve()
        target.relative_to(root)
    except Exception:
        return False, 0, f"Path '{rel_path}' escapes repository worktree root."
    if not target.exists() or not target.is_file():
        return False, 0, f"Cited file '{rel_path}' does not exist in worktree at head_sha."
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return False, 0, f"Could not read cited file '{rel_path}': {exc}"
    return True, max(1, len(lines)), ""


def parse_and_validate_review_output(
    raw_output: str,
    *,
    worktree_dir: Path,
    contract: dict[str, Any],
    reviewer_engine: str = "",
    reviewer_provider: str = "",
    reviewer_tier: str = "",
) -> ValidatedReviewResult:
    """Parse structured reviewer JSON and validate every ``file:line`` and ``omission`` citation."""
    parsed = _extract_json_object(raw_output)
    if parsed is None:
        return ValidatedReviewResult(
            valid=False,
            verdict="INVALID",
            errors=["Reviewer output is not valid JSON matching the required review schema."],
            reviewer_engine=reviewer_engine,
            reviewer_provider=reviewer_provider,
            reviewer_tier=reviewer_tier,
        )

    errors: list[str] = []
    verdict = str(parsed.get("verdict") or "").strip().upper()
    if verdict not in ("APPROVE", "CHANGES"):
        errors.append(f"Invalid verdict '{verdict}'; expected 'APPROVE' or 'CHANGES'.")

    summary = str(parsed.get("summary") or "").strip()
    raw_findings = parsed.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        errors.append("'findings' must be a JSON array.")
        raw_findings = []

    # Extract valid acceptance criteria IDs from frozen contract
    raw_ac = contract.get("acceptance_criteria_json")
    ac_list: list[Any] = []
    if isinstance(raw_ac, str) and raw_ac.strip():
        try:
            ac_list = json.loads(raw_ac)
        except Exception:
            ac_list = []
    elif isinstance(contract.get("acceptance_criteria"), list):
        ac_list = list(contract["acceptance_criteria"])
    valid_ac_ids = {
        str(item.get("id") or "").strip()
        for item in ac_list
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    validated_findings: list[ValidatedReviewFinding] = []
    for idx, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            errors.append(f"Finding #{idx + 1} is not a JSON object.")
            continue
        priority = str(item.get("priority") or "").strip().upper()
        if priority not in ("P1", "P2"):
            errors.append(f"Finding #{idx + 1} has invalid priority '{priority}'; expected 'P1' or 'P2'.")
            continue

        kind = str(item.get("kind") or "defect").strip().lower()
        if kind not in ("defect", "omission"):
            errors.append(f"Finding #{idx + 1} has invalid kind '{kind}'; expected 'defect' or 'omission'.")
            continue

        message = str(item.get("message") or "").strip()
        if not message:
            errors.append(f"Finding #{idx + 1} is missing a non-empty 'message'.")
            continue

        file_path = str(item.get("file") or "").strip()
        raw_line = item.get("line")
        ac_id = str(item.get("acceptance_criteria_id") or "").strip() or None

        if kind == "omission":
            if not ac_id:
                errors.append(f"Omission finding #{idx + 1} must cite 'acceptance_criteria_id'.")
                continue
            if valid_ac_ids and ac_id not in valid_ac_ids:
                errors.append(
                    f"Omission finding #{idx + 1} cites unknown acceptance_criteria_id '{ac_id}' "
                    f"(valid: {sorted(valid_ac_ids)})."
                )
                continue
            if not file_path or file_path.startswith("/") or ".." in file_path.replace("\\", "/").split("/"):
                errors.append(f"Omission finding #{idx + 1} must cite a valid relative target 'file' or module path.")
                continue
            line_num: int | None = None
            if raw_line is not None:
                if isinstance(raw_line, bool) or not isinstance(raw_line, int) or raw_line < 1:
                    errors.append(f"Omission finding #{idx + 1} has invalid line number '{raw_line}'.")
                    continue
                exists, line_count, err = _count_file_lines_safe(worktree_dir, file_path)
                if not exists:
                    errors.append(f"Omission finding #{idx + 1} cited line {raw_line} but {err}")
                    continue
                if raw_line > line_count:
                    errors.append(
                        f"Omission finding #{idx + 1} cites {file_path}:{raw_line}, "
                        f"but file has only {line_count} line(s)."
                    )
                    continue
                line_num = raw_line
            validated_findings.append(
                ValidatedReviewFinding(
                    priority=priority,
                    kind="omission",
                    file=normalize_repo_path(file_path, case_fold=False),
                    line=line_num,
                    message=message,
                    acceptance_criteria_id=ac_id,
                )
            )
        else:
            # kind == "defect": requires exact file:line existing in checked-out repo at head_sha
            if not file_path:
                errors.append(f"Finding #{idx + 1} is missing required 'file' citation.")
                continue
            if raw_line is None or isinstance(raw_line, bool) or not isinstance(raw_line, int) or raw_line < 1:
                errors.append(f"Finding #{idx + 1} is missing a valid positive integer 'line' citation.")
                continue
            exists, line_count, err = _count_file_lines_safe(worktree_dir, file_path)
            if not exists:
                errors.append(f"Finding #{idx + 1}: {err}")
                continue
            if raw_line > line_count:
                errors.append(
                    f"Finding #{idx + 1} cites {file_path}:{raw_line}, but file has only {line_count} line(s)."
                )
                continue
            validated_findings.append(
                ValidatedReviewFinding(
                    priority=priority,
                    kind="defect",
                    file=normalize_repo_path(file_path, case_fold=False),
                    line=raw_line,
                    message=message,
                    acceptance_criteria_id=ac_id,
                )
            )

    has_p1 = any(f.priority == "P1" for f in validated_findings)
    if verdict == "APPROVE" and has_p1:
        errors.append("Reviewer returned verdict='APPROVE' while reporting blocking P1 finding(s).")

    if errors:
        return ValidatedReviewResult(
            valid=False,
            verdict="INVALID",
            summary=summary,
            findings=validated_findings,
            errors=errors,
            reviewer_engine=reviewer_engine,
            reviewer_provider=reviewer_provider,
            reviewer_tier=reviewer_tier,
        )

    return ValidatedReviewResult(
        valid=True,
        verdict=verdict,
        summary=summary,
        findings=validated_findings,
        errors=[],
        reviewer_engine=reviewer_engine,
        reviewer_provider=reviewer_provider,
        reviewer_tier=reviewer_tier,
    )


# ---------------------------------------------------------------------------
# Provider-independent reviewer selection (Amendment G)
# ---------------------------------------------------------------------------

def select_independent_reviewer(
    *,
    contributing_providers: set[str],
    risk_class: str,
    runners: dict[str, Runner],
    is_available: Callable[[str], bool],
    candidate_order: list[str] | tuple[str, ...] = DEFAULT_REVIEWER_ORDER,
    engine_tiers: dict[str, str] | None = None,
    graduated_economy_engines: set[str] | None = None,
) -> tuple[str | None, Runner | None, str]:
    """Select an independent cross-provider reviewer (AGENT_PIPELINE §§2, 6.1, 9.2).

    Hard rules (NEVER relaxed, NEVER falls back to self-review):
    1. Excludes EVERY provider in ``contributing_providers`` (coders + fixers + handoffs;
       ``agy`` and ``gemini`` both map to ``google``).
    2. For ``high_risk`` and ``governance``, requires a Standard or Gold tier reviewer.
    3. Economy reviewers are eligible ONLY for non-high-risk classes AND when graduated.
    4. Returns ``(None, None, reason)`` when no independent reviewer is available.
    """
    norm_contributing = {provider_for_engine(p) for p in contributing_providers if p}
    policy = policy_for_risk_class(risk_class)
    graduated = set(graduated_economy_engines or set())
    tiers_lookup = engine_tiers or {}

    excluded_reasons: list[str] = []
    for cand in candidate_order:
        if cand not in runners:
            continue
        provider = provider_for_engine(cand)
        if provider in norm_contributing:
            excluded_reasons.append(f"{cand} (provider '{provider}' contributed to branch)")
            continue
        tier = tier_for_engine(cand, explicit_tier=tiers_lookup.get(cand))
        if policy.requires_strong_reviewer and tier not in STRONG_TIERS:
            excluded_reasons.append(f"{cand} (tier '{tier}' not allowed for {risk_class})")
            continue
        if tier == "economy" and cand not in graduated:
            excluded_reasons.append(f"{cand} (economy reviewer not benchmark-graduated)")
            continue
        if not is_available(cand):
            excluded_reasons.append(f"{cand} (unavailable/cooling down)")
            continue
        return cand, runners[cand], f"Selected independent {tier} reviewer {cand} (provider: {provider})"

    detail = "; ".join(excluded_reasons) if excluded_reasons else "no reviewer runners configured"
    return (
        None,
        None,
        f"No independent reviewer available outside contributing providers {sorted(norm_contributing)}: {detail}",
    )
