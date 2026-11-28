"""Stage 4 TRIAGE role on small_job_ladder (AGENT_PIPELINE §2, §7.2, §12.1 — Phase 8f).

Summarises failing CI / job log tails into a strict <= 10-line structured summary
containing root cause, file:line location, and suggested fix so Stage 5 FIX receives
compact, redacted context instead of raw multi-thousand-line logs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.clock import active_time
from nightshift.engine.pipeline_contracts import provider_for_engine
from nightshift.engine.pipeline_economics import PipelineEconomics, is_free_only_engine
from nightshift.engine.verify import redact_secrets
from nightshift.runners.base import Runner
from nightshift.store import Store

SMALL_JOB_LADDER_DEFAULT: tuple[str, ...] = (
    "qwen",
    "deepseek",
    "kimi",
    "glm",
    "minimax",
    "gemini",
    "codex",
    "agy",
)

MAX_TRIAGE_LINES: int = 10
MAX_LOG_TAIL_CHARS: int = 8000

_FILE_LINE_RE = re.compile(
    r"(?:File\s+\"([^\"]+)\",\s*line\s+(\d+)|"
    r"([A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|jsx|kt|java|go|rs|css|json)):(\d+))"
)
_ERROR_LINE_RE = re.compile(
    r"(?:AssertionError|TypeError|ValueError|ImportError|ModuleNotFoundError|SyntaxError|"
    r"FAILED|ERROR|Exception|TS\d{4}|error:|FAIL\s)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TriageSummary:
    """Structured Stage 4 TRIAGE output strictly bounded to <= 10 lines."""

    root_cause: str
    file_line: str
    suggested_fix: str
    lines: list[str] = field(default_factory=list)
    engine_id: str = ""
    model: str = ""
    used_fallback: bool = False
    wall_seconds: float = 0.0

    def render(self) -> str:
        """Render the triage summary as plain text guaranteed to be <= 10 non-empty lines."""
        raw_lines = [ln.strip() for ln in self.lines if ln.strip()]
        if not raw_lines:
            raw_lines = [
                f"Root cause: {self.root_cause}",
                f"Location: {self.file_line}",
                f"Suggested fix: {self.suggested_fix}",
            ]
        return "\n".join(raw_lines[:MAX_TRIAGE_LINES])

    @property
    def line_count(self) -> int:
        return len([ln for ln in self.render().splitlines() if ln.strip()])


def build_triage_prompt(failing_log: str, *, job_name: str = "CI") -> str:
    """Build a compact, secret-redacted Stage 4 TRIAGE prompt."""
    redacted = redact_secrets(failing_log or "")
    tail = redacted[-MAX_LOG_TAIL_CHARS:]
    return (
        f"You are the Stage 4 TRIAGE assistant for a failing {job_name} check.\n"
        "Read the log tail below and return ONLY valid JSON with these exact keys:\n"
        '  "root_cause": one concise sentence explaining why it failed,\n'
        '  "file_line": primary "path/to/file:line" (or "unknown:0" if not in log),\n'
        '  "suggested_fix": one concise sentence describing the targeted fix,\n'
        '  "notes": optional array of up to 4 short bullet strings.\n'
        "Total rendered summary must be <= 10 lines. Do not invent file paths or stack frames.\n\n"
        f"--- FAILING LOG TAIL ---\n{tail}\n"
    )


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from model output (raw or fenced)."""
    raw = (text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def parse_triage_output(raw_output: str) -> TriageSummary | None:
    """Parse and validate structured triage output, enforcing <= 10 lines."""
    obj = _extract_json_block(raw_output)
    if obj is None:
        return None
    root_cause = redact_secrets(str(obj.get("root_cause") or "").strip()).replace("\n", " ")
    file_line = redact_secrets(str(obj.get("file_line") or "").strip()).replace("\n", " ")
    suggested_fix = redact_secrets(str(obj.get("suggested_fix") or "").strip()).replace("\n", " ")
    if not root_cause or not file_line or not suggested_fix:
        return None

    lines = [
        f"Root cause: {root_cause}",
        f"Location: {file_line}",
        f"Suggested fix: {suggested_fix}",
    ]
    raw_notes = obj.get("notes")
    if isinstance(raw_notes, list):
        for note in raw_notes:
            cleaned = redact_secrets(str(note or "").strip()).replace("\n", " ")
            if cleaned and len(lines) < MAX_TRIAGE_LINES:
                lines.append(f"- {cleaned}")

    return TriageSummary(
        root_cause=root_cause,
        file_line=file_line,
        suggested_fix=suggested_fix,
        lines=lines[:MAX_TRIAGE_LINES],
    )


def deterministic_triage_fallback(failing_log: str, *, job_name: str = "CI") -> TriageSummary:
    """Deterministic fallback when no triage AI is available or output is malformed (always <= 10 lines)."""
    redacted = redact_secrets(failing_log or "")
    file_line = "unknown:0"
    for match in _FILE_LINE_RE.finditer(redacted):
        if match.group(1) and match.group(2):
            file_line = f"{match.group(1)}:{match.group(2)}"
            break
        if match.group(3) and match.group(4):
            file_line = f"{match.group(3)}:{match.group(4)}"
            break

    error_lines = [
        ln.strip()
        for ln in redacted.splitlines()
        if ln.strip() and _ERROR_LINE_RE.search(ln)
    ]
    root_cause = error_lines[-1][:200] if error_lines else f"{job_name} check failed without a specific error line."
    suggested_fix = f"Inspect {file_line} and resolve the failing assertion or error reported by {job_name}."
    lines = [
        f"Root cause: {root_cause}",
        f"Location: {file_line}",
        f"Suggested fix: {suggested_fix}",
    ]
    for extra in error_lines[-4:-1]:
        if len(lines) < MAX_TRIAGE_LINES:
            lines.append(f"- {extra[:180]}")

    return TriageSummary(
        root_cause=root_cause,
        file_line=file_line,
        suggested_fix=suggested_fix,
        lines=lines[:MAX_TRIAGE_LINES],
        engine_id="fallback",
        model="deterministic",
        used_fallback=True,
    )


def _has_engine_credentials(engine_id: str, eng: dict[str, Any] | None) -> bool:
    """Same credential sources the Router uses: the app's credential store (``api_key_<engine>``
    or ``api_key_<model>``), then the process environment."""
    import os

    from nightshift.engine.credentials import get_credential_store

    try:
        cred = get_credential_store()
        model = str((eng or {}).get("model") or "")
        if cred.get_secret(f"api_key_{engine_id}") or (model and cred.get_secret(f"api_key_{model}")):
            return True
    except Exception:
        pass
    return bool(os.environ.get(f"{engine_id.upper()}_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _filter_small_job_candidates(
    candidates: list[str],
    store: Store | None,
) -> list[str]:
    """Filter out disabled, uncredentialed, or free_only_mode-forbidden engines."""
    if store is None:
        return candidates
    economics = PipelineEconomics(store)
    free_only = economics.is_free_only_mode()
    engines_by_id = {str(e["id"]): e for e in store.list_engines()}
    latest_logins = store.get_latest_login_checks()
    filtered: list[str] = []
    for cand in candidates:
        eng = engines_by_id.get(cand)
        if eng is None:
            eng = next((e for e in engines_by_id.values() if str(e.get("runner") or "") == cand), None)
        if eng is not None and not int(eng.get("enabled", 1)):
            continue
        runner = str(eng.get("runner") or "") if eng is not None else ""
        tier = str(eng.get("cost_tier") or "") if eng is not None else ""
        if free_only and not is_free_only_engine(cand, runner=runner, cost_tier=tier):
            continue
        if runner == "claude-compatible" or cand in ("deepseek", "kimi", "glm", "minimax"):
            if not _has_engine_credentials(cand, eng):
                continue
        login = latest_logins.get(cand) or (latest_logins.get(runner) if runner else None)
        if login and str(login.get("state") or "") in ("Logged out", "Not installed", "Disabled by owner"):
            continue
        filtered.append(cand)
    return filtered


def get_small_job_ladder(
    store: Store | None = None,
    project_id: str | None = None,
) -> list[str]:
    """Return configured ``small_job_ladder`` for ``project_id``, filtered by availability & free_only_mode."""
    if store is not None:
        parsed = store.get_project_ladders(project_id)
        if isinstance(parsed, dict) and isinstance(parsed.get("small_job_ladder"), list):
            ladder: list[str] = []
            for x in parsed["small_job_ladder"]:
                if isinstance(x, dict):
                    val = str(x.get("runner") or x.get("engine") or x.get("id") or "").strip()
                else:
                    val = str(x).strip()
                if val and val not in ladder:
                    ladder.append(val)
            if ladder:
                return _filter_small_job_candidates(ladder, store)
    return _filter_small_job_candidates(list(SMALL_JOB_LADDER_DEFAULT), store)


async def run_triage_stage(
    *,
    failing_log: str,
    workdir: Path,
    runners: dict[str, Runner],
    store: Store | None = None,
    router: Any | None = None,
    task_id: str | None = None,
    risk_class: str = "business_logic",
    job_name: str = "CI",
    timeout_seconds: int = 120,
) -> TriageSummary:
    """Execute Stage 4 TRIAGE on ``small_job_ladder`` with strict routing safeguards.

    Every candidate (including fallbacks) MUST pass ``Router.route_pipeline_task``
    safeguards (enabled, cooldown, free_only_mode, hard budgets, high_risk/governance
    strong-tier rule, and Economy benchmark graduation). When no eligible runner
    remains, falls back deterministically without running any ineligible engine.
    """
    prompt = build_triage_prompt(failing_log, job_name=job_name)
    project_id = store.get_task_project_id(task_id) if (store is not None and task_id) else None
    ladder = get_small_job_ladder(store, project_id)
    economics = PipelineEconomics(store) if store is not None else None
    norm_risk = (risk_class or "business_logic").strip().lower()

    effective_router = router
    if effective_router is None and store is not None:
        from nightshift.engine.router import Router
        from nightshift.quota.ledger import QuotaLedger

        effective_router = Router(store, QuotaLedger(store), runners=runners)

    if effective_router is None or not hasattr(effective_router, "route_pipeline_task"):
        return deterministic_triage_fallback(failing_log, job_name=job_name)

    remaining_allowed: set[str] = set(ladder)
    excluded_candidates: set[str] = set()

    while remaining_allowed:
        route = effective_router.route_pipeline_task(
            task_id=task_id,
            role="triage",
            risk_class=norm_risk,
            contributing_providers=set(),
            allowed_engines=remaining_allowed,
            exclude_engines=excluded_candidates,
        )
        if route.blocked or not route.chosen_id:
            break

        candidate = route.chosen_id
        candidate_runner_name = route.runner or candidate
        excluded_candidates.add(candidate)
        excluded_candidates.add(candidate_runner_name)
        remaining_allowed.discard(candidate)
        remaining_allowed.discard(candidate_runner_name)

        runner = (
            runners.get(candidate)
            or runners.get(candidate_runner_name)
            or runners.get(candidate.split("-")[0])
        )
        if runner is None:
            continue

        t0 = active_time()
        res = await runner.run(prompt, workdir, timeout_seconds=timeout_seconds)
        elapsed = round(active_time() - t0, 3)
        parsed = parse_triage_output(res.stdout) if res.rc == 0 else None
        model_name = route.model or getattr(runner, "model", "") or candidate

        attempt_id: int | None = None
        if store is not None and task_id:
            attempt_id = store.record_task_attempt(
                task_id=task_id,
                stage="TRIAGE",
                role="triage",
                engine_id=candidate,
                provider=route.provider or provider_for_engine(candidate),
                model=model_name,
                input_ref="redacted:ci_log_tail",
                output_ref="redacted:triage_summary",
                decision_reason=route.reason or f"small_job_ladder triage ({candidate})",
                rc=res.rc,
                outcome="ok" if parsed is not None else "failed",
                wall_seconds=elapsed,
            )
        if economics is not None and task_id:
            # Record attempt-level success ("ok") or retry; task acceptance ("accepted")
            # is reserved strictly for verified delivery outcomes.
            economics.record_ledger_event(
                task_id=task_id,
                attempt_id=attempt_id,
                role="triage",
                engine_id=candidate,
                model=model_name,
                risk_class=norm_risk,
                tokens_in=len(prompt) // 4,
                tokens_out=len(res.stdout or "") // 4,
                wall_seconds=elapsed,
                outcome="ok" if parsed is not None else "retry",
            )

        if parsed is not None:
            return TriageSummary(
                root_cause=parsed.root_cause,
                file_line=parsed.file_line,
                suggested_fix=parsed.suggested_fix,
                lines=parsed.lines[:MAX_TRIAGE_LINES],
                engine_id=candidate,
                model=model_name,
                used_fallback=False,
                wall_seconds=elapsed,
            )

    return deterministic_triage_fallback(failing_log, job_name=job_name)
