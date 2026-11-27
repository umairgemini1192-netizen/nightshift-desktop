"""Trust boundaries, frozen requirement contracts, protected test authority, and MergeGate.

Implements Governance Amendments A, B, E and docs/AGENT_PIPELINE.md sections 1, 3, and 8
(Phase 8b: Trust boundaries + protected test authority).
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from nightshift.engine.verify import GuardFinding

# Canonical mapping from engine/CLI name to provider (AGENT_PIPELINE §2).
# Provider independence is enforced at the provider level (e.g. agy and gemini are both google).
ENGINE_PROVIDER_MAP: dict[str, str] = {
    "agy": "google",
    "antigravity": "google",
    "gemini": "google",
    "codex": "openai",
    "gpt": "openai",
    "claude": "anthropic",
    "qwen": "alibaba",
    "deepseek": "deepseek",
    "kimi": "moonshot",
    "glm": "zhipu",
    "minimax": "minimax",
    "script": "local",
}


def provider_for_engine(engine_name: str) -> str:
    """Resolve the canonical provider name for an engine ID or CLI name."""
    raw = (engine_name or "").strip().lower()
    if not raw:
        return ""
    if raw in ENGINE_PROVIDER_MAP:
        return ENGINE_PROVIDER_MAP[raw]
    # Handle composite engine IDs like "eng-agy-neon", "claude-sonnet", "codex-gpt"
    for key, provider in ENGINE_PROVIDER_MAP.items():
        if raw.startswith(f"{key}-") or raw.startswith(f"{key}_") or f"-{key}" in raw:
            return provider
    return raw


# Governance-classified path globs (AGENT_PIPELINE §3.2)
GOVERNANCE_PATH_GLOBS: tuple[str, ...] = (
    "**/tests/**",
    "tests/**",
    "**/__tests__/**",
    "__tests__/**",
    "**/*_test.py",
    "*_test.py",
    "**/test_*.py",
    "test_*.py",
    "**/conftest.py",
    "conftest.py",
    "**/*.test.ts",
    "*.test.ts",
    "**/*.test.tsx",
    "*.test.tsx",
    "**/*.spec.ts",
    "*.spec.ts",
    "**/*.spec.tsx",
    "*.spec.tsx",
    "**/*.test.js",
    "*.test.js",
    "**/*.test.jsx",
    "*.test.jsx",
    "**/*.spec.js",
    "*.spec.js",
    "**/*.spec.jsx",
    "*.spec.jsx",
    "pytest.ini",
    "**/pytest.ini",
    "vitest.config.*",
    "**/vitest.config.*",
    "jest.config.*",
    "**/jest.config.*",
    "playwright.config.*",
    "**/playwright.config.*",
    ".circleci/**",
    ".github/workflows/**",
    "backend/packaging/**",
    "scripts/**",
    "design/tokens.json",
    "docs/DESIGN_SYSTEM.md",
    "frontend/src/design/tokens.ts",
    "frontend/src/design/tokens.css",
    "AGENTS.md",
    "GEMINI.md",
    ".nightshift/contracts/**",
)

TEST_FILE_GLOBS: tuple[str, ...] = (
    "**/tests/**/*.py",
    "tests/**/*.py",
    "**/*_test.py",
    "*_test.py",
    "**/test_*.py",
    "test_*.py",
    "**/conftest.py",
    "conftest.py",
    "**/*.test.ts",
    "*.test.ts",
    "**/*.test.tsx",
    "*.test.tsx",
    "**/*.spec.ts",
    "*.spec.ts",
    "**/*.spec.tsx",
    "*.spec.tsx",
    "**/*.test.js",
    "*.test.js",
    "**/*.test.jsx",
    "*.test.jsx",
    "**/*.spec.js",
    "*.spec.js",
    "**/*.spec.jsx",
    "*.spec.jsx",
)

DEFAULT_FORBIDDEN_GLOBS: tuple[str, ...] = (
    ".circleci/**",
    ".github/**",
    "scripts/check_*",
    "design/tokens.json",
    "docs/DESIGN_SYSTEM.md",
)

RISK_CLASS_ORDER: dict[str, int] = {
    "docs_format": 0,
    "ui_cosmetic": 1,
    "business_logic": 2,
    "high_risk": 3,
    "governance": 4,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_repo_path(path: str, *, case_fold: bool | None = None) -> str:
    """Normalize a repository-relative path to POSIX '/' separators.

    On Windows (or when ``case_fold=True``), paths are case-folded so case-variant
    bypasses like ``Scripts/Check_Tokens.py`` or ``CONFTEST.PY`` cannot slip past globs.
    """
    norm = path.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    should_fold = (sys.platform == "win32") if case_fold is None else case_fold
    return norm.casefold() if should_fold else norm


def _glob_matches(path: str, pattern: str, *, case_fold: bool | None = None) -> bool:
    """Match POSIX-normalized repo paths against a glob supporting ``**``."""
    p = normalize_repo_path(path, case_fold=case_fold)
    pat = normalize_repo_path(pattern, case_fold=case_fold)
    if fnmatch.fnmatchcase(p, pat):
        return True
    if pat.startswith("**/") and fnmatch.fnmatchcase(p, pat[3:]):
        return True
    if "/**/" in pat and fnmatch.fnmatchcase(p, pat.replace("/**/", "/")):
        return True
    if pat.endswith("/**"):
        prefix = pat[:-3]
        if p == prefix or p.startswith(prefix + "/"):
            return True
    return False


@dataclass(frozen=True)
class DiffPathEntry:
    """Single entry parsed from ``git diff --name-status -M -C``."""

    status: str  # A, M, D, R, C, T, U
    path: str
    old_path: str | None = None

    def all_paths(self) -> list[str]:
        if self.old_path:
            return [self.old_path, self.path]
        return [self.path]


def parse_name_status_diff(output: str, *, nul_delimited: bool | None = None) -> list[DiffPathEntry]:
    """Parse ``git diff --name-status -M -C [-z]`` output preserving both old and new paths.

    When ``nul_delimited`` is True (or auto-detected from NUL bytes in ``output``),
    paths are extracted verbatim without stripping or interpreting filename characters.
    """
    use_nul = ("\0" in output) if nul_delimited is None else nul_delimited
    entries: list[DiffPathEntry] = []
    if use_nul:
        tokens = output.split("\0")
        if tokens and tokens[-1] == "":
            tokens.pop()
        idx = 0
        total = len(tokens)
        while idx < total:
            code = tokens[idx]
            if not code:
                idx += 1
                continue
            kind = code[0].upper()
            if kind in ("R", "C"):
                if idx + 2 >= total:
                    break
                entries.append(DiffPathEntry(status=kind, old_path=tokens[idx + 1], path=tokens[idx + 2]))
                idx += 3
            else:
                if idx + 1 >= total:
                    break
                entries.append(DiffPathEntry(status=kind, path=tokens[idx + 1]))
                idx += 2
        return entries

    for raw_line in output.splitlines():
        if not raw_line or not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        if len(parts) < 2:
            parts = raw_line.split()
        if len(parts) < 2:
            continue
        code = parts[0].strip()
        kind = code[0].upper()
        if kind in ("R", "C") and len(parts) >= 3:
            entries.append(DiffPathEntry(status=kind, old_path=parts[1], path=parts[2]))
        else:
            entries.append(DiffPathEntry(status=kind, path=parts[1]))
    return entries


def canonical_contract_json(payload: dict[str, Any]) -> str:
    """Serialize a task contract payload to canonical JSON (sorted keys, compact, excluding contract_sha256)."""
    clean = {k: v for k, v in payload.items() if k != "contract_sha256"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_contract_sha256(payload: dict[str, Any] | str) -> str:
    """Compute SHA-256 hex digest of a contract payload or canonical JSON string."""
    canonical = payload if isinstance(payload, str) else canonical_contract_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_contract_integrity(contract_row: dict[str, Any], expected_sha256: str | None = None) -> tuple[bool, str]:
    """Verify a stored contract row's canonical_json matches its stored and task-level SHA-256."""
    canonical = str(contract_row.get("canonical_json") or "")
    stored_sha = str(contract_row.get("contract_sha256") or "")
    if not canonical or not stored_sha:
        return False, "Contract is missing canonical_json or contract_sha256."
    recomputed = compute_contract_sha256(canonical)
    if recomputed != stored_sha:
        return False, f"Contract digest mismatch: recomputed {recomputed[:12]} != stored {stored_sha[:12]}."
    if expected_sha256 and stored_sha != expected_sha256:
        return False, f"Task contract digest {expected_sha256[:12]} != frozen contract {stored_sha[:12]}."
    try:
        parsed = json.loads(canonical)
        if "contract_sha256" in parsed:
            return False, "Canonical contract JSON must not embed contract_sha256."
    except Exception as exc:
        return False, f"Invalid canonical_json: {exc}"
    return True, "Contract integrity verified."


def is_governance_path(
    path: str,
    *,
    extra_protected_paths: list[str] | None = None,
    diff_content: str = "",
    case_fold: bool | None = None,
) -> bool:
    """Return True if ``path`` touches protected test authority, CI, gates, or design contracts."""
    norm = normalize_repo_path(path, case_fold=case_fold)
    globs = list(GOVERNANCE_PATH_GLOBS) + list(extra_protected_paths or [])
    for pat in globs:
        if _glob_matches(norm, pat, case_fold=case_fold):
            return True
    # AGENT_PIPELINE §3.2: [tool.pytest*] edits in pyproject.toml
    if norm.endswith("pyproject.toml") and diff_content:
        if re.search(r"^[+\-\s]*\[tool\.pytest", diff_content, re.MULTILINE | re.IGNORECASE):
            return True
    return False


def is_test_file_path(path: str, *, case_fold: bool | None = None) -> bool:
    """Return True if ``path`` matches a known Python or JS/TS test file pattern."""
    norm = normalize_repo_path(path, case_fold=case_fold)
    return any(_glob_matches(norm, pat, case_fold=case_fold) for pat in TEST_FILE_GLOBS)


def check_contract_path_guards(
    entries: list[DiffPathEntry],
    *,
    allowed_globs: list[str],
    forbidden_globs: list[str] | None = None,
    extra_protected_paths: list[str] | None = None,
    diff_content: str = "",
    case_fold: bool | None = None,
) -> tuple[list[GuardFinding], bool]:
    """Validate changed/renamed/deleted paths against allowed_globs, forbidden_globs, and governance rules.

    Evaluates BOTH old and new paths for renames (R), copies (C), and deletions (D).
    Returns ``(findings, touches_governance)``.
    """
    findings: list[GuardFinding] = []
    forbidden = list(forbidden_globs) if forbidden_globs is not None else list(DEFAULT_FORBIDDEN_GLOBS)
    governance_paths: list[str] = []
    feature_paths: list[str] = []

    for entry in entries:
        for raw_path in entry.all_paths():
            norm = normalize_repo_path(raw_path, case_fold=case_fold)
            if is_governance_path(
                raw_path,
                extra_protected_paths=extra_protected_paths,
                diff_content=diff_content,
                case_fold=case_fold,
            ):
                if norm not in governance_paths:
                    governance_paths.append(norm)
            else:
                if norm not in feature_paths:
                    feature_paths.append(norm)

            for fpat in forbidden:
                if _glob_matches(raw_path, fpat, case_fold=case_fold):
                    findings.append(
                        GuardFinding(
                            guard="outside_allowed_globs",
                            message=f"Path '{raw_path}' matches forbidden glob '{fpat}'.",
                            path=raw_path,
                        )
                    )
                    break
            else:
                if allowed_globs and not any(
                    _glob_matches(raw_path, apat, case_fold=case_fold) for apat in allowed_globs
                ):
                    findings.append(
                        GuardFinding(
                            guard="outside_allowed_globs",
                            message=f"Path '{raw_path}' is outside contract allowed_globs.",
                            path=raw_path,
                        )
                    )

    touches_governance = len(governance_paths) > 0
    if touches_governance and feature_paths:
        findings.append(
            GuardFinding(
                guard="governance_split_required",
                message=(
                    f"Mixed feature and governance diff is forbidden "
                    f"(governance: {', '.join(governance_paths[:3])}; feature: {', '.join(feature_paths[:3])}). "
                    f"Split into a separate Governance PR."
                ),
                path=governance_paths[0],
            )
        )

    return findings, touches_governance


def _is_pytest_skip_or_xfail_decorator(dec: ast.AST) -> str | None:
    """Return marker name if ``dec`` is ``@pytest.mark.skip`` or ``@pytest.mark.xfail``."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute) and target.attr in ("skip", "xfail", "skipif"):
        if isinstance(target.value, ast.Attribute) and target.value.attr == "mark":
            return f"@pytest.mark.{target.attr}"
    return None


def _analyze_python_tests(source: str) -> tuple[dict[str, int], int, dict[str, int]]:
    """Return ``({qualified_test_fn: assertion_count}, broad_except_pass_count, skip_marker_counts)``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}, 0, {}

    counts: dict[str, int] = {}
    broad_except_pass = 0
    skip_markers: dict[str, int] = {}

    def _count_in_node(node: ast.AST) -> int:
        total = 0
        for child in ast.walk(node):
            if isinstance(child, ast.Assert):
                total += 1
            elif isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute):
                    if func.attr.startswith("assert") or func.attr == "raises":
                        total += 1
                elif isinstance(func, ast.Name) and func.id.startswith("assert"):
                    total += 1
        return total

    def _visit(node: ast.AST, prefix: str = "") -> None:
        nonlocal broad_except_pass
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                _visit(child, prefix=f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    qual_name = f"{prefix}{child.name}"
                    counts[qual_name] = _count_in_node(child)
                for dec in child.decorator_list:
                    marker = _is_pytest_skip_or_xfail_decorator(dec)
                    if marker:
                        skip_markers[marker] = skip_markers.get(marker, 0) + 1
                _visit(child, prefix=f"{prefix}{child.name}.")
            else:
                if isinstance(child, ast.ExceptHandler):
                    is_broad = child.type is None or (
                        isinstance(child.type, ast.Name) and child.type.id in ("Exception", "BaseException")
                    )
                    if is_broad and len(child.body) == 1 and isinstance(child.body[0], ast.Pass):
                        broad_except_pass += 1
                _visit(child, prefix=prefix)

    _visit(tree)
    return counts, broad_except_pass, skip_markers


def detect_python_assertion_relaxation(
    path: str,
    before_source: str | None,
    after_source: str | None,
) -> list[GuardFinding]:
    """AST-based detector for deleted tests, weakened assertions, skips, or broadened except:pass."""
    findings: list[GuardFinding] = []
    if before_source is not None and after_source is None:
        findings.append(
            GuardFinding(
                guard="assertion_relaxation",
                message=f"Deleted test file '{path}' blocks merge.",
                path=path,
            )
        )
        return findings

    before_counts, before_broad, before_skips = _analyze_python_tests(before_source or "")
    after_counts, after_broad, after_skips = _analyze_python_tests(after_source or "")

    for fn_name, prev_n in before_counts.items():
        if fn_name not in after_counts:
            findings.append(
                GuardFinding(
                    guard="assertion_relaxation",
                    message=f"Deleted test function '{fn_name}' in '{path}'.",
                    path=path,
                )
            )
        elif after_counts[fn_name] < prev_n:
            findings.append(
                GuardFinding(
                    guard="assertion_relaxation",
                    message=(
                        f"Net decrease in assertions in '{fn_name}' ({prev_n} -> {after_counts[fn_name]}) "
                        f"in '{path}'."
                    ),
                    path=path,
                )
            )

    if after_broad > before_broad:
        findings.append(
            GuardFinding(
                guard="assertion_relaxation",
                message=f"Introduced broad 'except Exception: pass' in test file '{path}'.",
                path=path,
            )
        )

    for marker in ("@pytest.mark.skip", "@pytest.mark.xfail", "@pytest.mark.skipif"):
        if after_skips.get(marker, 0) > before_skips.get(marker, 0):
            findings.append(
                GuardFinding(
                    guard="assertion_relaxation",
                    message=f"Introduced '{marker}' in test file '{path}'.",
                    path=path,
                )
            )

    # Also catch raw pytest.skip(...) / pytest.xfail(...) calls introduced in test bodies
    after_text = after_source or ""
    before_text = before_source or ""
    for call_pat in ("pytest.skip(", "pytest.xfail("):
        if after_text.count(call_pat) > before_text.count(call_pat):
            findings.append(
                GuardFinding(
                    guard="assertion_relaxation",
                    message=f"Introduced '{call_pat})' in test file '{path}'.",
                    path=path,
                )
            )

    return findings


_STRICT_JS_MATCHERS = re.compile(
    r"\.(?:toBe|toEqual|toStrictEqual|toHaveText|toContain|toHaveLength|toMatch|toHaveAttribute)\s*\("
)
_VACUOUS_JS_MATCHERS = re.compile(r"\.(?:toBeDefined|toBeTruthy)\s*\(")
_JS_EXPECT_CALL = re.compile(r"\bexpect\s*\(")
_JS_SKIP_MARKER = re.compile(r"\b(?:test|it|describe)\.(?:skip|only|fixme)\b")
_JS_AXE_CHECK = re.compile(r"\bAxeBuilder\b|\baxe\b", re.IGNORECASE)


def detect_js_assertion_relaxation(
    path: str,
    before_source: str | None,
    after_source: str | None,
) -> list[GuardFinding]:
    """Static detector for weakened Vitest/Jest/Playwright assertions."""
    findings: list[GuardFinding] = []
    if before_source is not None and after_source is None:
        findings.append(
            GuardFinding(
                guard="assertion_relaxation",
                message=f"Deleted JS/TS test file '{path}' blocks merge.",
                path=path,
            )
        )
        return findings

    before = before_source or ""
    after = after_source or ""

    prev_expects = len(_JS_EXPECT_CALL.findall(before))
    next_expects = len(_JS_EXPECT_CALL.findall(after))
    if next_expects < prev_expects:
        findings.append(
            GuardFinding(
                guard="assertion_relaxation",
                message=f"Net decrease in expect() calls ({prev_expects} -> {next_expects}) in '{path}'.",
                path=path,
            )
        )

    prev_strict = len(_STRICT_JS_MATCHERS.findall(before))
    next_strict = len(_STRICT_JS_MATCHERS.findall(after))
    prev_vacuous = len(_VACUOUS_JS_MATCHERS.findall(before))
    next_vacuous = len(_VACUOUS_JS_MATCHERS.findall(after))
    if next_strict < prev_strict and next_vacuous > prev_vacuous:
        findings.append(
            GuardFinding(
                guard="assertion_relaxation",
                message=f"Replaced strict matcher with vacuous matcher (toBeDefined/toBeTruthy) in '{path}'.",
                path=path,
            )
        )

    if len(_JS_SKIP_MARKER.findall(after)) > len(_JS_SKIP_MARKER.findall(before)):
        findings.append(
            GuardFinding(
                guard="assertion_relaxation",
                message=f"Introduced .skip/.only/test.fixme in '{path}'.",
                path=path,
            )
        )

    if len(_JS_AXE_CHECK.findall(after)) < len(_JS_AXE_CHECK.findall(before)):
        findings.append(
            GuardFinding(
                guard="assertion_relaxation",
                message=f"Removed axe accessibility check in '{path}'.",
                path=path,
            )
        )

    return findings


def detect_assertion_relaxation_for_files(
    file_snapshots: dict[str, tuple[str | None, str | None]],
    *,
    case_fold: bool | None = None,
) -> list[GuardFinding]:
    """Run assertion-relaxation detection across all modified/deleted test files."""
    findings: list[GuardFinding] = []
    for path, (before_src, after_src) in file_snapshots.items():
        if not is_test_file_path(path, case_fold=case_fold):
            continue
        low = path.lower()
        if low.endswith(".py"):
            findings.extend(detect_python_assertion_relaxation(path, before_src, after_src))
        elif low.endswith((".ts", ".tsx", ".js", ".jsx")):
            findings.extend(detect_js_assertion_relaxation(path, before_src, after_src))
    return findings


@dataclass(frozen=True)
class MergeAuthorization:
    """Result of ``MergeGate.authorize_merge``."""

    allowed: bool
    reason: str
    block_reason: str = ""
    findings: list[GuardFinding] = field(default_factory=list)


class MergeGate:
    """Single mandatory merge-gate chokepoint (AGENT_PIPELINE §§1, 3, 9).

    Every merge path in Night-Shift must pass through ``authorize_merge``.
    Autonomous merging is disabled by default until Phases 8b-8e reach
    ``Integration-tested`` maturity.
    """

    def __init__(
        self,
        *,
        autonomous_merge_enabled: bool = False,
        require_independent_reviewer: bool = True,
    ) -> None:
        self.autonomous_merge_enabled = autonomous_merge_enabled
        self.require_independent_reviewer = require_independent_reviewer

    def authorize_merge(
        self,
        *,
        task: dict[str, Any],
        contract: dict[str, Any],
        head_sha: str,
        expected_base_sha: str | None = None,
        current_base_sha: str | None = None,
        diff_entries: list[DiffPathEntry] | None = None,
        diff_content: str = "",
        test_file_snapshots: dict[str, tuple[str | None, str | None]] | None = None,
        evidence_records: list[dict[str, Any]] | None = None,
        reviewer_provider: str | None = None,
        extra_protected_paths: list[str] | None = None,
        case_fold: bool | None = None,
    ) -> MergeAuthorization:
        """Authorize or block merging ``head_sha`` for ``task``."""
        if not head_sha:
            return MergeAuthorization(
                False,
                "Cannot merge without an exact head_sha.",
                block_reason="missing_head_sha",
            )

        # 1. Verify frozen contract integrity
        ok_contract, contract_msg = verify_contract_integrity(
            contract,
            expected_sha256=str(task.get("contract_sha256") or ""),
        )
        if not ok_contract:
            return MergeAuthorization(
                False,
                contract_msg,
                block_reason="contract_integrity_failed",
            )

        # 2. Race-safe base branch check (AGENT_PIPELINE §9.1)
        if expected_base_sha and current_base_sha and expected_base_sha != current_base_sha:
            return MergeAuthorization(
                False,
                f"Base branch moved ({expected_base_sha[:7]} -> {current_base_sha[:7]}); update branch and re-verify.",
                block_reason="base_branch_moved",
            )

        # 3. Diff path guards (allowed_globs, forbidden_globs, governance classification & split)
        allowed_globs = json.loads(str(contract.get("allowed_globs_json") or "[]"))
        forbidden_globs = json.loads(str(contract.get("forbidden_globs_json") or "[]"))
        entries = diff_entries or []
        path_findings, touches_governance = check_contract_path_guards(
            entries,
            allowed_globs=allowed_globs,
            forbidden_globs=forbidden_globs,
            extra_protected_paths=extra_protected_paths,
            diff_content=diff_content,
            case_fold=case_fold,
        )
        effective_risk = (
            "governance"
            if touches_governance
            else str(task.get("risk_class") or contract.get("risk_class") or "business_logic")
        )

        # 4. Blocking assertion-relaxation check (applies unconditionally to feature AND governance PRs)
        relax_findings = detect_assertion_relaxation_for_files(test_file_snapshots or {}, case_fold=case_fold)
        if relax_findings:
            waiver_sha = str(task.get("assertion_waiver_sha") or "")
            waiver_reason = str(task.get("assertion_waiver_reason") or "").strip()
            has_valid_waiver = (
                effective_risk == "governance"
                and waiver_sha == head_sha
                and bool(waiver_reason)
            )
            if not has_valid_waiver:
                return MergeAuthorization(
                    False,
                    "Deleted or relaxed test assertions block merge unconditionally "
                    "(requires exact-SHA owner waiver in a dedicated governance PR).",
                    block_reason="assertion_relaxation",
                    findings=relax_findings,
                )

        if path_findings:
            return MergeAuthorization(
                False,
                "Path guard blocked merge: " + "; ".join(f.message for f in path_findings),
                block_reason=path_findings[0].guard,
                findings=path_findings,
            )

        # 5. Provider independence check (AGENT_PIPELINE §2 & §9.2)
        if self.require_independent_reviewer:
            contributing = {
                provider_for_engine(p)
                for p in json.loads(str(task.get("contributing_providers_json") or "[]"))
                if p
            }
            norm_reviewer = provider_for_engine(reviewer_provider or "")
            if norm_reviewer and norm_reviewer in contributing:
                return MergeAuthorization(
                    False,
                    f"Reviewer provider '{norm_reviewer}' is in contributing_providers {sorted(contributing)}; "
                    f"self-approval is forbidden.",
                    block_reason="reviewer_not_independent",
                )
            if effective_risk in ("business_logic", "high_risk", "governance"):
                if not norm_reviewer:
                    return MergeAuthorization(
                        False,
                        f"Risk class '{effective_risk}' requires an independent cross-provider review.",
                        block_reason="no_independent_reviewer",
                    )

        # 6. Exact-SHA verified evidence check (when evidence records are provided)
        if evidence_records is not None:
            valid_ci = [
                rec
                for rec in evidence_records
                if rec.get("kind") == "ci_check"
                and rec.get("commit_sha") == head_sha
                and rec.get("contract_sha256") == contract.get("contract_sha256")
                and bool(rec.get("verified"))
            ]
            if not valid_ci:
                return MergeAuthorization(
                    False,
                    f"No verified exact-SHA CI evidence found for commit {head_sha[:7]}.",
                    block_reason="missing_verified_evidence",
                )

        # 7. Governance and high_risk ALWAYS require explicit owner approval on exact head_sha
        owner_required = (
            effective_risk in ("governance", "high_risk")
            or bool(contract.get("owner_approval_required"))
        )
        owner_approved_sha = str(task.get("owner_approved_sha") or "")
        if owner_required and owner_approved_sha != head_sha:
            return MergeAuthorization(
                False,
                f"Risk class '{effective_risk}' requires explicit owner approval for commit {head_sha[:7]}.",
                block_reason="awaiting_owner_approval",
            )

        # 8. Staged rollout safety rule (AGENT_PIPELINE §1)
        if not self.autonomous_merge_enabled and owner_approved_sha != head_sha:
            return MergeAuthorization(
                False,
                "Autonomous merge is disabled during staged rollout; awaiting explicit owner approval.",
                block_reason="awaiting_owner_approval",
            )

        return MergeAuthorization(True, f"Merge authorized for {head_sha[:7]} ({effective_risk}).")
