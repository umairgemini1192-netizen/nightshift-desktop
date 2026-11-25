"""AI Planning Council (BLUEPRINT 5b, Plan Studio).

Coordinates the 3-role planning council:
  1. Draft: Gemini (Gemini CLI) - drafts phases as JSON matching the plan schema
  2. Critique: GPT (Codex CLI) - reviews draft for missing steps, unsafe ordering, oversized phases (>1 day), vague tests
  3. Final review: Claude (Claude CLI, single short call, skipped automatically when budget pressure is high)
     - resolves disagreements, checks BLUEPRINT rules, outputs final plan + trade-offs summary.

Roles use substitution through the Router if assigned AI is unavailable or cooling down.
Schema validation with 1 retry on invalid JSON including the validation error.
All role outputs and usage estimates are persisted in `council_runs`.
Repo snapshot via `repomix --compress` if available, else built-in compact tree + summary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nightshift.engine.planner import PhaseDefinition, validate_plan, validate_plan_dag
from nightshift.engine.router import Router, SIZE_LARGE, TIER_GOLD
from nightshift.quota.usage import parse_cli_usage
from nightshift.runners import (
    AgyRunner,
    ClaudeRunner,
    CodexRunner,
    GeminiRunner,
    Runner,
    RunnerResult,
    TemplateRunner,
)
from nightshift.runners.base import run_subprocess_with_timeout
from nightshift.store import Store


@dataclass
class CouncilRoleResult:
    role: str  # "draft", "critique", "final"
    engine_id: str
    runner: str
    model: str
    substituted: bool = False
    substitution_reason: str = ""
    raw_output: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    phases: list[dict[str, Any]] = field(default_factory=list)
    critique_issues: list[str] = field(default_factory=list)
    tradeoff_summary: str = ""
    notes: list[str] = field(default_factory=list)
    usage_estimate: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    skipped: bool = False
    skip_reason: str = ""
    retry_count: int = 0


@dataclass
class CouncilSessionResult:
    goal: str
    repo_path: str
    plan_id: str
    draft: CouncilRoleResult
    critique: CouncilRoleResult
    final: CouncilRoleResult
    diff_summary: str
    approved_plan: dict[str, Any]
    total_tokens_estimate: int = 0
    total_cost_usd_estimate: float = 0.0


def extract_json_payload(text: str) -> dict[str, Any]:
    """Extract and parse JSON object from raw markdown fences or plain text."""
    clean = text.strip()

    # Search for markdown fenced JSON blocks
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))

    # Search for first '{' and matching last '}'
    first_brace = clean.find("{")
    last_brace = clean.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = clean[first_brace : last_brace + 1]
        return json.loads(candidate)

    raise ValueError("No JSON object found in response")


def generate_repo_snapshot(repo_path: Path, max_chars: int = 14000) -> str:
    """Generate compact repo snapshot via repomix or built-in file tree + summaries."""
    if not repo_path.exists():
        return f"Repository root: {repo_path}"

    repomix_bin = shutil.which("repomix") or shutil.which("repomix.cmd")
    if repomix_bin:
        try:
            rc, out, err, timed_out = run_subprocess_with_timeout(
                [repomix_bin, "--compress", "--stdout"],
                cwd=repo_path,
                timeout_seconds=30,
            )
            if rc == 0 and out.strip():
                return out.strip()[:max_chars]
        except Exception:
            pass

    # Built-in compact tree + key file summaries
    ignored = {".git", "node_modules", "dist", "build", ".venv", "__pycache__", ".idea", ".vscode"}
    tree_lines: list[str] = [f"Repo: {repo_path.name}"]
    count = 0

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in ignored and not d.startswith(".")]
        rel = Path(root).relative_to(repo_path)
        if len(rel.parts) > 3:
            continue
        indent = "  " * len(rel.parts)
        if rel.parts:
            tree_lines.append(f"{indent}{rel.parts[-1]}/")
        for f in files[:8]:
            if count > 80:
                break
            if not f.startswith(".") and not f.endswith((".pyc", ".png", ".jpg", ".ico", ".apk")):
                tree_lines.append(f"{indent}  {f}")
                count += 1

    summary_parts: list[str] = ["\n".join(tree_lines[:70])]

    for key_file in ("README.md", "BLUEPRINT.md", "package.json", "pyproject.toml"):
        kp = repo_path / key_file
        if kp.exists():
            try:
                content = kp.read_text(encoding="utf-8", errors="replace")[:1200]
                summary_parts.append(f"\n--- {key_file} ---\n{content}")
            except Exception:
                continue

    full_snapshot = "\n".join(summary_parts)
    return full_snapshot[:max_chars]


class PlanningCouncil:
    """AI Planning Council coordinator (Gemini draft -> GPT critique -> Claude final)."""

    def __init__(
        self,
        store: Store,
        router: Router,
        runner_factory: Callable[[str, str], Runner] | None = None,
    ) -> None:
        self.store = store
        self.router = router
        self.runner_factory = runner_factory or self._default_runner_factory

    def _default_runner_factory(self, runner_name: str, engine_id: str) -> Runner:
        ledger = self.router.quota_ledger
        if runner_name == "gemini":
            return GeminiRunner(ledger)
        if runner_name == "codex":
            return CodexRunner(ledger)
        if runner_name == "claude":
            return ClaudeRunner(ledger)
        if runner_name == "agy":
            return AgyRunner(ledger)
        # Template runner for economy / custom engines
        eng = self.store.get_engine(engine_id) or {}
        tmpl = eng.get("command_template") or [runner_name, "-p", "{instruction}"]
        env = self.router.build_isolated_environment(engine_id)
        patterns_raw = eng.get("quota_patterns_json") or "[]"
        try:
            patterns = json.loads(patterns_raw)
        except Exception:
            patterns = []
        return TemplateRunner(runner_name, tmpl, quota_ledger=ledger, quota_patterns=patterns, env=env)

    def _resolve_role_engine(
        self,
        role: str,
        preferred_id: str,
        exclude_engines: set[str] | None = None,
    ) -> tuple[str, str, str, bool, str]:
        """Resolve engine for a council role via Router with automatic substitution."""
        excluded = set(exclude_engines or set())
        eng = self.store.get_engine(preferred_id)
        if eng:
            avail, rej = self.router.check_availability(eng, excluded)
            if avail:
                return eng["id"], eng["runner"], eng.get("model", ""), False, ""

        # Engine unavailable -> ask Router to pick next best Standard/Gold planner
        decision = self.router.pick(
            step_kind="plan",
            size=SIZE_LARGE,
            exclude_engines=excluded.union({preferred_id}),
        )
        reason = f"{preferred_id} unavailable; substituted with {decision.chosen_id}"
        return decision.chosen_id, decision.runner, decision.model, True, reason

    async def _execute_role_with_retry(
        self,
        role: str,
        runner: Runner,
        prompt: str,
        workdir: Path,
        model: str,
        validate_fn: Callable[[dict[str, Any]], tuple[bool, str, dict[str, Any]]],
    ) -> tuple[dict[str, Any], str, int, dict[str, Any]]:
        """Run role prompt with strict validation and 1 retry on invalid JSON."""
        retries = 0
        current_prompt = prompt

        while retries <= 1:
            res: RunnerResult = await runner.run(current_prompt, workdir, model=model)
            raw = res.stdout if res.rc == 0 or res.stdout else res.stderr
            parsed_in, parsed_out = parse_cli_usage(raw)
            est_in = parsed_in if parsed_in > 0 else max(1, len(current_prompt) // 4)
            est_out = parsed_out if parsed_out > 0 else max(1, len(raw) // 4)
            est_cost = round(((est_in + est_out) / 1_000_000) * 3.0, 4)
            usage_dict = {
                "tokens_in": est_in,
                "tokens_out": est_out,
                "cost_usd": est_cost,
                "is_estimate": parsed_in == 0 and parsed_out == 0,
            }
            if getattr(res, "rc", 0) == 0:
                self.router.quota_ledger.record_usage(
                    getattr(runner, "name", role),
                    model or "default",
                    tokens_in=est_in,
                    tokens_out=est_out,
                    event=f"council_{role}",
                )

            try:
                parsed = extract_json_payload(raw)
                valid, err_msg, validated_plan = validate_fn(parsed)
                if valid:
                    return validated_plan, raw, retries, usage_dict
                else:
                    if retries == 0:
                        retries += 1
                        current_prompt = (
                            f"{prompt}\n\n"
                            f"IMPORTANT ERROR FIX: Your previous response had validation error: {err_msg}.\n"
                            f"Please fix and return strictly valid JSON matching the exact schema."
                        )
                        continue
                    else:
                        # Fallback after 1 retry
                        return validated_plan or parsed, raw, retries, usage_dict
            except Exception as exc:
                if retries == 0:
                    retries += 1
                    current_prompt = (
                        f"{prompt}\n\n"
                        f"IMPORTANT ERROR FIX: Your previous response was not valid JSON ({exc}).\n"
                        f"Return ONLY valid JSON wrapped in ```json ... ``` without extra text."
                    )
                    continue
                else:
                    return {"phases": [], "error": str(exc)}, raw, retries, usage_dict

        return {"phases": []}, "", retries, {}

    def _validate_phases_schema(self, data: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Validate that parsed dict contains a valid list of phases."""
        raw_phases = data.get("phases")
        if not isinstance(raw_phases, list) or not raw_phases:
            return False, "Plan must contain a non-empty 'phases' list", data

        normalized_phases: list[dict[str, Any]] = []
        phase_defs: list[PhaseDefinition] = []

        for idx, p in enumerate(raw_phases, start=1):
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or f"phase-{idx}")
            title = str(p.get("title") or pid)
            branch = str(p.get("branch") or f"feat/{pid}")
            prompt = str(p.get("prompt") or "")
            deps = list(p.get("depends_on") or [])
            size = str(p.get("size") or "medium")
            timeout = int(p.get("timeout_minutes") or 180)
            ladder = str(p.get("ladder") or "default")

            norm = {
                "id": pid,
                "title": title,
                "branch": branch,
                "prompt": prompt,
                "depends_on": deps,
                "size": size,
                "timeout_minutes": timeout,
                "ladder": ladder,
                "implementer": str(p.get("implementer") or "auto"),
                "acceptance_tests": list(p.get("acceptance_tests") or []),
                "auto_merge": bool(p.get("auto_merge", True)),
                "release_after_merge": bool(p.get("release_after_merge", False)),
            }
            normalized_phases.append(norm)
            phase_defs.append(PhaseDefinition.from_dict(norm))

        plan_out: dict[str, Any] = {"phases": normalized_phases}
        if data.get("title"):
            plan_out["title"] = str(data["title"])
        problems = validate_plan(phase_defs)
        if problems:
            return False, problems[0], plan_out

        return True, "", plan_out

    async def run_draft(
        self,
        goal: str,
        repo_path: Path,
        exclude_engines: set[str] | None = None,
    ) -> CouncilRoleResult:
        """Role 1: Draft - Gemini (or next best planner)."""
        t0 = time.time()
        eng_id, runner_name, model, substituted, sub_reason = self._resolve_role_engine("draft", "gemini", exclude_engines)
        runner = self.runner_factory(runner_name, eng_id)

        snapshot = generate_repo_snapshot(repo_path)
        prompt = (
            f"You are the Lead Architect drafting an implementation plan for this goal:\n"
            f"GOAL: {goal}\n\n"
            f"REPOSITORY SNAPSHOT:\n{snapshot}\n\n"
            f"Draft a phased implementation plan. Return strictly a JSON object with this structure:\n"
            f"```json\n"
            f'{{\n  "title": "Short plan title",\n  "phases": [\n'
            f'    {{\n      "id": "p1-short-name",\n      "title": "Title",\n      "branch": "feat/p1-name",\n'
            f'      "prompt": "Specific instructions for AI coder",\n      "depends_on": [],\n'
            f'      "size": "medium",\n      "timeout_minutes": 180\n    }}\n  ]\n}}\n'
            f"```"
        )

        plan, raw, retries, usage = await self._execute_role_with_retry(
            "draft", runner, prompt, repo_path, model, self._validate_phases_schema
        )
        elapsed = round(time.time() - t0, 2)

        return CouncilRoleResult(
            role="draft",
            engine_id=eng_id,
            runner=runner_name,
            model=model,
            substituted=substituted,
            substitution_reason=sub_reason,
            raw_output=raw,
            plan=plan,
            phases=plan.get("phases", []),
            notes=[f"Drafted {len(plan.get('phases', []))} phases based on goal and repo snapshot."],
            usage_estimate=usage,
            elapsed_seconds=elapsed,
            retry_count=retries,
        )

    async def run_critique(
        self,
        goal: str,
        draft_plan: dict[str, Any],
        repo_path: Path,
        exclude_engines: set[str] | None = None,
    ) -> CouncilRoleResult:
        """Role 2: Critique - GPT / Codex (or next best planner)."""
        t0 = time.time()
        eng_id, runner_name, model, substituted, sub_reason = self._resolve_role_engine("critique", "codex", exclude_engines)
        runner = self.runner_factory(runner_name, eng_id)

        draft_json_str = json.dumps(draft_plan, indent=2)
        prompt = (
            f"You are the Senior Reviewer critiquing an implementation plan.\n"
            f"GOAL: {goal}\n\n"
            f"DRAFT PLAN:\n{draft_json_str}\n\n"
            f"Review this draft for: missing steps, unsafe dependency ordering, phases too big for one PR (>1 day), vague acceptance tests.\n"
            f"Return strictly a JSON object with issues list and revised phases:\n"
            f"```json\n"
            f'{{\n  "issues": ["Issue 1 description", "Issue 2 description"],\n'
            f'  "phases": [ ... revised phases ... ]\n}}\n'
            f"```"
        )

        def validate_critique(data: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
            issues = data.get("issues") or []
            val, err, validated_phases = self._validate_phases_schema(data)
            validated_phases["issues"] = [str(i) for i in issues]
            return val, err, validated_phases

        plan, raw, retries, usage = await self._execute_role_with_retry(
            "critique", runner, prompt, repo_path, model, validate_critique
        )
        elapsed = round(time.time() - t0, 2)
        issues = plan.get("issues", [])

        return CouncilRoleResult(
            role="critique",
            engine_id=eng_id,
            runner=runner_name,
            model=model,
            substituted=substituted,
            substitution_reason=sub_reason,
            raw_output=raw,
            plan=plan,
            phases=plan.get("phases", []),
            critique_issues=issues,
            notes=[f"Identified {len(issues)} critique issue(s) and refined phases."],
            usage_estimate=usage,
            elapsed_seconds=elapsed,
            retry_count=retries,
        )

    async def run_final(
        self,
        goal: str,
        draft_phases: list[dict[str, Any]],
        critique_phases: list[dict[str, Any]],
        critique_issues: list[str],
        repo_path: Path,
        exclude_engines: set[str] | None = None,
    ) -> CouncilRoleResult:
        """Role 3: Final review - Claude (skipped if budget pressure is high)."""
        t0 = time.time()
        claude_eng = self.store.get_engine("claude")
        claude_budget = claude_eng.get("budget_json", "{}") if claude_eng else "{}"
        pressure = self.router.calculate_budget_pressure("claude", claude_budget)

        # Check if Claude should be skipped due to budget pressure or cooldown
        if (
            pressure >= 0.70
            or not self.router.quota_ledger.check_budget_cap("claude", claude_budget)
            or self.router.quota_ledger.is_cooling_down("claude")
        ):
            # Skip Claude automatically to save gold quota
            skip_msg = f"Claude skipped automatically (budget pressure {int(pressure * 100)}% / gold quota preserved)"
            tradeoff = "Accepted critique revisions directly to preserve gold budget."
            return CouncilRoleResult(
                role="final",
                engine_id="claude",
                runner="claude",
                model="claude-3-7-sonnet",
                skipped=True,
                skip_reason=skip_msg,
                plan={"phases": critique_phases or draft_phases},
                phases=critique_phases or draft_phases,
                tradeoff_summary=tradeoff,
                notes=[skip_msg, tradeoff],
                elapsed_seconds=0.1,
            )

        eng_id, runner_name, model, substituted, sub_reason = self._resolve_role_engine("final", "claude", exclude_engines)
        runner = self.runner_factory(runner_name, eng_id)

        prompt = (
            f"You are the Principal Engineer conducting final plan review.\n"
            f"GOAL: {goal}\n\n"
            f"DRAFT PHASES: {json.dumps(draft_phases)}\n"
            f"CRITIQUE ISSUES: {json.dumps(critique_issues)}\n"
            f"CRITIQUE REVISED PHASES: {json.dumps(critique_phases)}\n\n"
            f"Resolve any disagreements and produce the final, definitive plan with a plain-English trade-off summary.\n"
            f"Return strictly a JSON object:\n"
            f"```json\n"
            f'{{\n  "tradeoff_summary": "Plain English summary of trade-offs and architectural decisions",\n'
            f'  "phases": [ ... final phases ... ]\n}}\n'
            f"```"
        )

        def validate_final(data: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
            tradeoff = data.get("tradeoff_summary") or "Final plan validated and approved."
            val, err, validated_phases = self._validate_phases_schema(data)
            validated_phases["tradeoff_summary"] = str(tradeoff)
            return val, err, validated_phases

        plan, raw, retries, usage = await self._execute_role_with_retry(
            "final", runner, prompt, repo_path, model, validate_final
        )
        elapsed = round(time.time() - t0, 2)
        tradeoff = plan.get("tradeoff_summary") or "Completed final review and balanced risk vs delivery speed."

        return CouncilRoleResult(
            role="final",
            engine_id=eng_id,
            runner=runner_name,
            model=model,
            substituted=substituted,
            substitution_reason=sub_reason,
            raw_output=raw,
            plan=plan,
            phases=plan.get("phases", []),
            tradeoff_summary=tradeoff,
            notes=[tradeoff],
            usage_estimate=usage,
            elapsed_seconds=elapsed,
            retry_count=retries,
        )

    async def run_council(
        self,
        goal: str,
        repo_path: Path,
        plan_id: str | None = None,
        project_id: str | None = None,
        exclude_engines: set[str] | None = None,
    ) -> CouncilSessionResult:
        """Run the full 3-role Planning Council session."""
        pid = plan_id or f"plan-{int(time.time())}"

        # Ensure parent plan (and project) exist so council_runs foreign key is satisfied
        if self.store.get_plan(pid) is None:
            proj_id = project_id
            if not proj_id:
                existing_projects = self.store.list_projects()
                if existing_projects:
                    proj_id = existing_projects[0]["id"]
                else:
                    proj_id = "default"
                    self.store.create_project(proj_id, repo_path.name or "Default", "owner/repo", str(repo_path))
            elif self.store.get_project(proj_id) is None:
                self.store.create_project(proj_id, repo_path.name or proj_id, "owner/repo", str(repo_path))
            self.store.create_plan(pid, proj_id, f"Council Plan: {goal[:40]}", goal=goal)

        # 1. Draft
        draft_res = await self.run_draft(goal, repo_path, exclude_engines)
        self.store.record_council_run(
            plan_id=pid,
            role="draft",
            engine=draft_res.engine_id,
            input_text=goal,
            output_text=json.dumps(draft_res.plan),
            usage_estimate=draft_res.usage_estimate,
        )

        # 2. Critique
        critique_res = await self.run_critique(goal, draft_res.plan, repo_path, exclude_engines)
        self.store.record_council_run(
            plan_id=pid,
            role="critique",
            engine=critique_res.engine_id,
            input_text=json.dumps(draft_res.plan),
            output_text=json.dumps(critique_res.plan),
            usage_estimate=critique_res.usage_estimate,
        )

        # 3. Final
        final_res = await self.run_final(
            goal,
            draft_res.phases,
            critique_res.phases,
            critique_res.critique_issues,
            repo_path,
            exclude_engines,
        )
        self.store.record_council_run(
            plan_id=pid,
            role="final",
            engine=final_res.engine_id,
            input_text=json.dumps(critique_res.plan),
            output_text=json.dumps(final_res.plan),
            usage_estimate=final_res.usage_estimate,
        )

        # Generate diff summary
        diff_lines = [
            f"Draft created {len(draft_res.phases)} phase(s).",
            f"Critique flagged {len(critique_res.critique_issues)} issue(s) -> refined to {len(critique_res.phases)} phase(s).",
        ]
        if final_res.skipped:
            diff_lines.append(f"Final review: {final_res.skip_reason}.")
        else:
            diff_lines.append(f"Final review confirmed {len(final_res.phases)} phase(s) with trade-off analysis.")
        diff_summary = " ".join(diff_lines)

        final_phases = final_res.phases or critique_res.phases or draft_res.phases
        approved_plan = {
            "id": pid,
            "title": f"Council Plan: {goal[:40]}",
            "goal": goal,
            "phases": final_phases,
            "tradeoff_summary": final_res.tradeoff_summary,
        }

        total_tokens = sum(
            int(r.usage_estimate.get("tokens_in", 0)) + int(r.usage_estimate.get("tokens_out", 0))
            for r in (draft_res, critique_res, final_res)
        )
        total_cost = round(
            sum(float(r.usage_estimate.get("cost_usd", 0.0)) for r in (draft_res, critique_res, final_res)), 3
        )

        return CouncilSessionResult(
            goal=goal,
            repo_path=str(repo_path),
            plan_id=pid,
            draft=draft_res,
            critique=critique_res,
            final=final_res,
            diff_summary=diff_summary,
            approved_plan=approved_plan,
            total_tokens_estimate=total_tokens,
            total_cost_usd_estimate=total_cost,
        )
