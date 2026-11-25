"""Smart, quota-aware router and Economy-First engine selection (BLUEPRINT 5a, 5c, 5h).

Implements:
- Engine catalogue with skill scores (1-5), cost tiers, owner budgets, and command templates
- Economy first routing: Economy (Qwen, DeepSeek, Kimi, GLM, MiniMax) -> Standard (Antigravity, Gemini, Codex) -> Gold (Claude)
- Deterministic scoring: skill * reliability * (1 - budget_pressure) - cost_penalty
- Job sizing (small, medium, large, critical) and escalation ladder
- Auto-promotion rule: if economy engine success rate < 60% over 10 jobs, start one tier higher
- Built-in engine templates with environment isolation for Claude-compatible endpoints
- Decision persistence to router_decisions with a one-line plain-English explanation
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nightshift.engine.credentials import get_credential_store, redact_known_secrets
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.base import run_subprocess_with_timeout
from nightshift.store import Store

# Cost tiers
TIER_ECONOMY = "economy"
TIER_STANDARD = "standard"
TIER_GOLD = "gold"

TIER_ORDER = [TIER_ECONOMY, TIER_STANDARD, TIER_GOLD]

# Sizing categories
SIZE_SMALL = "small"
SIZE_MEDIUM = "medium"
SIZE_LARGE = "large"
SIZE_CRITICAL = "critical"

# Wall-clock timeouts per tier (seconds)
TIER_TIMEOUTS = {
    SIZE_SMALL: 45 * 60,   # 45 min
    SIZE_MEDIUM: 90 * 60,  # 90 min
    SIZE_LARGE: 180 * 60,  # 180 min
    SIZE_CRITICAL: 240 * 60,
}

# Default Anthropic-compatible API base URLs for Economy engines (BLUEPRINT 5h)
DEFAULT_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/anthropic",
    "kimi": "https://api.moonshot.ai/anthropic",
    "glm": "https://open.bigmodel.cn/api/anthropic",
    "minimax": "https://api.minimax.io/anthropic",
}

# Seed default catalogue
SEED_ENGINES: list[dict[str, Any]] = [
    {
        "id": "agy-gemini-4-neon",
        "runner": "agy",
        "model": "gemini-4-neon",
        "effort": "medium",
        "cost_tier": TIER_STANDARD,
        "skills": {"ui": 5, "backend": 4, "fix_ci": 4, "plan": 3, "review": 3, "docs": 3},
        "budget": {"max_runs_today": 30},
        "command_template": ["agy", "-p", "{instruction}", "--dangerously-skip-permissions", "--model", "{model}", "--effort", "{effort}"],
        "quota_patterns": ["rate limit", "quota exceeded", "resource_exhausted"],
    },
    {
        "id": "agy-gemini-4-hydrogen",
        "runner": "agy",
        "model": "gemini-4-hydrogen",
        "effort": "medium",
        "cost_tier": TIER_STANDARD,
        "skills": {"backend": 5, "fix_ci": 4, "ui": 3, "plan": 3, "review": 3, "docs": 3},
        "budget": {"max_runs_today": 30},
        "command_template": ["agy", "-p", "{instruction}", "--dangerously-skip-permissions", "--model", "{model}", "--effort", "{effort}"],
        "quota_patterns": ["rate limit", "quota exceeded", "resource_exhausted"],
    },
    {
        "id": "gemini",
        "runner": "gemini",
        "model": "gemini-2.5-pro",
        "effort": "medium",
        "cost_tier": TIER_STANDARD,
        "skills": {"plan": 5, "docs": 4, "review": 4, "backend": 3, "ui": 3, "fix_ci": 3},
        "budget": {"max_runs_today": 50},
        "command_template": ["gemini", "-p", "{instruction}", "--approval-mode=yolo", "--skip-trust", "--model", "{model}"],
        "quota_patterns": ["rate limit", "quota exceeded", "resource_exhausted"],
    },
    {
        "id": "codex",
        "runner": "codex",
        "model": "o3-mini",
        "effort": "medium",
        "cost_tier": TIER_STANDARD,
        "skills": {"review": 5, "plan": 4, "fix_ci": 4, "backend": 4, "ui": 3, "docs": 3},
        "budget": {"max_runs_today": 25},
        "command_template": ["codex", "exec", "--sandbox", "workspace-write", "--approve-for-me", "{instruction}", "--model", "{model}"],
        "quota_patterns": ["rate limit", "usage limit", "429"],
    },
    {
        "id": "claude",
        "runner": "claude",
        "model": "claude-3-7-sonnet",
        "effort": "medium",
        "cost_tier": TIER_GOLD,
        "skills": {"review": 5, "plan": 5, "backend": 5, "ui": 4, "fix_ci": 4, "docs": 4},
        "budget": {"weekly_percent": 20, "max_runs_today": 5},
        "command_template": ["claude", "-p", "{instruction}", "--permission-mode", "bypassPermissions", "--output-format", "text", "--model", "{model}", "--effort", "{effort}"],
        "quota_patterns": ["rate limit", "usage limit", "credit balance"],
    },
    # Economy Engines (BLUEPRINT 5h)
    {
        "id": "qwen",
        "runner": "qwen",
        "model": "qwen-2.5-coder-32b",
        "effort": "medium",
        "cost_tier": TIER_ECONOMY,
        "skills": {"backend": 4, "fix_ci": 3, "ui": 3, "docs": 3, "plan": 2, "review": 2},
        "budget": {},
        "command_template": ["qwen", "-p", "{instruction}", "--approval-mode=yolo"],
        "quota_patterns": ["rate limit", "quota error", "capacity"],
    },
    {
        "id": "deepseek",
        "runner": "claude-compatible",
        "model": "deepseek-chat",
        "effort": "medium",
        "cost_tier": TIER_ECONOMY,
        "skills": {"backend": 4, "fix_ci": 4, "review": 3, "docs": 3, "plan": 3, "ui": 2},
        "budget": {},
        "command_template": ["claude", "-p", "{instruction}", "--permission-mode", "bypassPermissions", "--output-format", "text"],
        "quota_patterns": ["rate limit", "insufficient_quota", "429"],
    },
    {
        "id": "kimi",
        "runner": "claude-compatible",
        "model": "kimi-k2",
        "effort": "medium",
        "cost_tier": TIER_ECONOMY,
        "skills": {"docs": 4, "plan": 3, "review": 3, "backend": 3, "ui": 2, "fix_ci": 2},
        "budget": {},
        "command_template": ["claude", "-p", "{instruction}", "--permission-mode", "bypassPermissions", "--output-format", "text"],
        "quota_patterns": ["rate limit", "quota error"],
    },
    {
        "id": "glm",
        "runner": "claude-compatible",
        "model": "glm-4",
        "effort": "medium",
        "cost_tier": TIER_ECONOMY,
        "skills": {"docs": 4, "review": 3, "backend": 3, "plan": 2, "ui": 2, "fix_ci": 2},
        "budget": {},
        "command_template": ["claude", "-p", "{instruction}", "--permission-mode", "bypassPermissions", "--output-format", "text"],
        "quota_patterns": ["rate limit", "quota error"],
    },
    {
        "id": "minimax",
        "runner": "claude-compatible",
        "model": "minimax-text-01",
        "effort": "medium",
        "cost_tier": TIER_ECONOMY,
        "skills": {"docs": 4, "ui": 3, "backend": 2, "plan": 2, "review": 2, "fix_ci": 2},
        "budget": {},
        "command_template": ["claude", "-p", "{instruction}", "--permission-mode", "bypassPermissions", "--output-format", "text"],
        "quota_patterns": ["rate limit", "quota error"],
    },
]


def seed_default_engines(store: Store) -> None:
    """Populate default engine catalogue in the database if empty."""
    existing = {e["id"] for e in store.list_engines()}
    for eng in SEED_ENGINES:
        if eng["id"] not in existing:
            store.upsert_engine(
                engine_id=eng["id"],
                runner=eng["runner"],
                model=eng["model"],
                effort=eng.get("effort", "medium"),
                skills_json=json.dumps(eng.get("skills", {})),
                cost_tier=eng.get("cost_tier", TIER_STANDARD),
                budget_json=json.dumps(eng.get("budget", {})),
                enabled=1,
                pinned=0,
                command_template=json.dumps(eng.get("command_template", [])),
                usage_parser="",
                quota_patterns_json=json.dumps(eng.get("quota_patterns", [])),
            )


@dataclass(frozen=True)
class RouterScore:
    engine_id: str
    runner: str
    model: str
    cost_tier: str
    skill_score: float
    reliability: float
    budget_pressure: float
    cost_penalty: float
    final_score: float
    available: bool
    rejection_reason: str | None = None


@dataclass(frozen=True)
class RouterDecision:
    chosen_id: str
    runner: str
    model: str
    effort: str
    cost_tier: str
    reason: str
    scores: dict[str, float] = field(default_factory=dict)
    is_fallback: bool = False
    is_switchback: bool = False
    exhausted: bool = False


class Router:
    """Smart quota-aware router implementing BLUEPRINT 5a, 5c, and 5h."""

    def __init__(
        self,
        store: Store,
        quota_ledger: QuotaLedger,
        login_checker: Any | None = None,
        cred_store: Any | None = None,
    ) -> None:
        self.store = store
        self.quota_ledger = quota_ledger
        self.login_checker = login_checker
        self.cred_store = cred_store or get_credential_store()
        seed_default_engines(store)

    def is_engine_installed(self, runner: str) -> bool:
        """Check if runner CLI executable is present in PATH or tools folder."""
        if runner in ("claude-compatible", "claude"):
            return shutil.which("claude") is not None or shutil.which("claude.cmd") is not None
        if runner == "agy":
            return shutil.which("agy") is not None or shutil.which("agy.cmd") is not None
        if runner == "gemini":
            return shutil.which("gemini") is not None or shutil.which("gemini.cmd") is not None
        if runner == "codex":
            return shutil.which("codex") is not None or shutil.which("codex.cmd") is not None
        if runner == "qwen":
            return shutil.which("qwen") is not None or shutil.which("qwen.cmd") is not None
        return shutil.which(runner) is not None

    def check_availability(
        self,
        engine_dict: dict[str, Any],
        exclude_engines: set[str] | None = None,
    ) -> tuple[bool, str]:
        """Check if an engine is available according to CLI, logins, cooldown, and budget."""
        eng_id = engine_dict.get("id", "")
        runner = engine_dict.get("runner", "")
        model = engine_dict.get("model", "")

        if not engine_dict.get("enabled", 1):
            return False, f"{eng_id} is disabled by owner"

        if exclude_engines and (eng_id in exclude_engines or runner in exclude_engines):
            return False, f"{eng_id} is excluded for this step"

        # Check cooldown
        if self.quota_ledger.is_cooling_down(runner, model):
            reset_at = self.quota_ledger.get_reset_at(runner, model)
            time_str = reset_at.strftime("%H:%M") if reset_at else "later"
            return False, f"{runner} is cooling down until {time_str}"

        # Check budget (both this engine row and any runner-wide budget cap row with empty model)
        budget_raw = engine_dict.get("budget_json") or "{}"
        if not self.quota_ledger.check_budget_cap(runner, budget_raw):
            return False, f"{runner} reached its budget limit for tonight"
        for other in self.store.list_engines():
            if other.get("runner") == runner and not other.get("model"):
                if not self.quota_ledger.check_budget_cap(runner, other.get("budget_json") or "{}"):
                    return False, f"{runner} reached its budget limit for tonight"

        # Check login health
        if self.login_checker is not None:
            tool_name = "claude" if runner == "claude-compatible" else runner
            status = self.login_checker.check_tool(tool_name, in_run=True)
            if status.state in ("Logged out", "Not installed") or status.state.startswith("Limit reached"):
                return False, f"{runner} login status: {status.state}"

        # Check CLI binary exists
        if not self.is_engine_installed(runner):
            return False, f"{runner} CLI executable not found on PATH"

        # Check required API key for claude-compatible economy engines
        if runner == "claude-compatible":
            key = self.cred_store.get_secret(f"api_key_{eng_id}") or self.cred_store.get_secret(f"api_key_{model}")
            if not key:
                # If no custom key set, only available if generic ANTHROPIC_AUTH_TOKEN exists
                if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                    return False, f"No API key configured for {eng_id}"

        return True, "Available"

    def calculate_failure_rate(self, engine_id: str) -> float:
        """Calculate recent failure rate from engine_stats or steps table (0.0 to 1.0)."""
        stats = self.store.get_engine_stats()
        eng_stats = [s for s in stats if s.get("engine") == engine_id]
        if eng_stats:
            total_attempts = sum(s.get("attempts", 0) for s in eng_stats)
            total_success = sum(s.get("successes", 0) for s in eng_stats)
            if total_attempts >= 3:
                return max(0.0, min(1.0, 1.0 - (total_success / total_attempts)))
        return 0.0

    def calculate_budget_pressure(self, runner: str, budget_json: str) -> float:
        """Estimate budget pressure between 0.0 (no usage) and 1.0 (exhausted)."""
        try:
            b_data = json.loads(budget_json) if budget_json else {}
        except Exception:
            b_data = {}
        max_runs = b_data.get("max_runs_today")
        if not max_runs:
            return 0.0
        entries = self.store.get_ledger_entries(runner=runner, limit=200)
        today = datetime.now(timezone.utc).date().isoformat()
        used = len([e for e in entries if str(e.get("created_at", "")).startswith(today)])
        return min(1.0, used / float(max_runs))

    def is_auto_promoted(self, size: str) -> tuple[bool, str]:
        """Check if Economy tier is auto-promoted to Standard due to low success rate (<60% over 10 jobs)."""
        stats = self.store.get_engine_stats()
        economy_ids = {e["id"] for e in self.store.list_engines() if e.get("cost_tier") == TIER_ECONOMY}
        relevant = [s for s in stats if s.get("engine") in economy_ids and s.get("size") == size]
        if not relevant:
            return False, ""
        total_att = sum(s.get("attempts", 0) for s in relevant)
        total_suc = sum(s.get("successes", 0) for s in relevant)
        if total_att >= 10 and (total_suc / total_att) < 0.60:
            rate_pct = int((total_suc / total_att) * 100)
            return True, f"Economy success rate dropped to {rate_pct}% for {size} jobs (auto-promoted to Standard)"
        return False, ""

    def score_engines(
        self,
        step_kind: str,
        target_size: str = SIZE_MEDIUM,
        exclude_engines: set[str] | None = None,
    ) -> list[RouterScore]:
        """Compute deterministic score for all registered engines."""
        engines = self.store.list_engines()
        scores: list[RouterScore] = []

        # Check auto-promotion for economy tier
        auto_promoted, _ = self.is_auto_promoted(target_size)

        effective_kind = {"implement": "backend", "ci_fix": "fix_ci"}.get(step_kind, step_kind)

        for eng in engines:
            eng_id = eng["id"]
            runner = eng["runner"]
            model = eng.get("model", "")
            cost_tier = eng.get("cost_tier", TIER_STANDARD)

            skills_raw = eng.get("skills_json", "{}")
            try:
                skills = json.loads(skills_raw)
            except Exception:
                skills = {}

            skill_score = float(skills.get(effective_kind, skills.get(step_kind, 3)))
            available, rej_reason = self.check_availability(eng, exclude_engines)

            if not available:
                scores.append(
                    RouterScore(
                        engine_id=eng_id,
                        runner=runner,
                        model=model,
                        cost_tier=cost_tier,
                        skill_score=skill_score,
                        reliability=0.0,
                        budget_pressure=1.0,
                        cost_penalty=10.0,
                        final_score=-999.0,
                        available=False,
                        rejection_reason=rej_reason,
                    )
                )
                continue

            failure_rate = self.calculate_failure_rate(eng_id)
            reliability = 1.0 - failure_rate

            budget_pressure = self.calculate_budget_pressure(runner, eng.get("budget_json", "{}"))

            # Cost penalty calculation (BLUEPRINT 5a, 5h)
            if cost_tier == TIER_ECONOMY:
                cost_penalty = 0.0
                if auto_promoted:
                    cost_penalty = 3.0  # Penalize economy when auto-promoted
            elif cost_tier == TIER_STANDARD:
                cost_penalty = 1.0
            else:  # GOLD (Claude)
                # Claude carries a strong cost penalty: 4.5
                cost_penalty = 4.5

            # Formula: skill * reliability * (1 - budget_pressure) - cost_penalty
            final_score = (skill_score * reliability * (1.0 - (0.5 * budget_pressure))) - cost_penalty

            # Owner pinned engine overrides score
            if eng.get("pinned", 0):
                final_score += 100.0

            scores.append(
                RouterScore(
                    engine_id=eng_id,
                    runner=runner,
                    model=model,
                    cost_tier=cost_tier,
                    skill_score=skill_score,
                    reliability=reliability,
                    budget_pressure=budget_pressure,
                    cost_penalty=cost_penalty,
                    final_score=round(final_score, 3),
                    available=True,
                )
            )

        # Sort deterministically by final_score descending, then tier order, then engine_id
        scores.sort(
            key=lambda s: (
                -s.final_score,
                TIER_ORDER.index(s.cost_tier) if s.cost_tier in TIER_ORDER else 9,
                s.engine_id,
            )
        )
        return scores

    def pick(
        self,
        step_kind: str,
        phase_id: str | None = None,
        *,
        size: str = SIZE_MEDIUM,
        current_engine: str | None = None,
        exclude_engines: set[str] | None = None,
        step_id: int | None = None,
    ) -> RouterDecision:
        """Select the best engine for a step, honouring owner overrides and Economy-first rules."""
        excluded = set(exclude_engines or set())

        # 1. Owner phase assignment check (BLUEPRINT 5c)
        if phase_id:
            assignment = self.store.get_phase_assignment(phase_id)
            if assignment:
                preferred = assignment.get("override_engine") or assignment.get("planned_engine")
                if preferred and preferred != "auto":
                    # Map common aliases
                    preferred = {"Antigravity": "agy", "Gemini CLI": "gemini", "Codex CLI": "codex", "Claude Code": "claude"}.get(preferred, preferred)
                    if preferred not in excluded:
                        eng_match = next((e for e in self.store.list_engines() if e["id"] == preferred or e["runner"] == preferred), None)
                        if eng_match:
                            avail, rej = self.check_availability(eng_match, excluded)
                            if avail:
                                is_override = bool(assignment.get("override_engine"))
                                is_sb = bool(current_engine and current_engine != preferred)
                                prefix = "Owner override" if is_override else "Plan implementer"
                                reason = f"{prefix} {preferred} active" + (" (fast switch-back)" if is_sb else "")
                                decision = RouterDecision(
                                    chosen_id=eng_match["id"],
                                    runner=eng_match["runner"],
                                    model=assignment.get("model") or eng_match["model"],
                                    effort=assignment.get("effort") or eng_match.get("effort", "medium"),
                                    cost_tier=eng_match.get("cost_tier", TIER_STANDARD),
                                    reason=reason,
                                    scores={eng_match["id"]: 99.0},
                                    is_switchback=is_sb,
                                )
                                self.store.record_router_decision(
                                    chosen=decision.chosen_id,
                                    reason=decision.reason,
                                    step_id=step_id,
                                    scores_json=json.dumps(decision.scores),
                                )
                                return decision

        # 2. Economy-first starting tier based on size (BLUEPRINT 5h)
        # Sizing: small -> Economy, medium -> Economy first, large/critical -> Standard first
        auto_promoted, auto_prom_reason = self.is_auto_promoted(size)
        preferred_tier = TIER_STANDARD if (size in (SIZE_LARGE, SIZE_CRITICAL) or auto_promoted) else TIER_ECONOMY

        scored = self.score_engines(step_kind, target_size=size, exclude_engines=excluded)
        available = [s for s in scored if s.available]

        if not available:
            # Fall back to any engine even with penalties if available
            all_avail = [s for s in self.score_engines(step_kind, target_size=size, exclude_engines=set()) if s.available]
            if all_avail:
                best = all_avail[0]
                reason = f"{best.engine_id} selected as emergency fallback (all priority engines cooling down)"
            else:
                # Absolute fallback (all engines unavailable/exhausted)
                reason = "All engines cooling down; defaulting to Antigravity as fallback"
                decision = RouterDecision(
                    chosen_id="agy",
                    runner="agy",
                    model="gemini-4-neon",
                    effort="medium",
                    cost_tier=TIER_STANDARD,
                    reason=reason,
                    is_fallback=True,
                    exhausted=True,
                )
                self.store.record_router_decision(
                    chosen=decision.chosen_id,
                    reason=decision.reason,
                    step_id=step_id,
                )
                return decision
        else:
            # Pick highest score matching preferred tier, or next tier if preferred exhausted
            tier_candidates = [s for s in available if s.cost_tier == preferred_tier]
            if not tier_candidates and preferred_tier == TIER_ECONOMY:
                # Escalate to Standard
                tier_candidates = [s for s in available if s.cost_tier == TIER_STANDARD]
            if not tier_candidates:
                # Escalate to any available
                tier_candidates = available

            best = tier_candidates[0]
            scores_dict = {s.engine_id: s.final_score for s in available[:4]}

            # Formulate plain-English reason
            if best.cost_tier == TIER_ECONOMY:
                reason = f"{best.engine_id} chosen: Economy tier for {size} job"
            elif auto_promoted:
                reason = f"{best.engine_id} chosen: {auto_prom_reason}"
            elif best.cost_tier == TIER_GOLD:
                reason = f"Claude chosen: high skill ({best.skill_score}) within Gold budget"
            else:
                top_alt = next((s for s in scored if not s.available), None)
                if top_alt and top_alt.rejection_reason:
                    reason = f"{best.engine_id} chosen: {top_alt.rejection_reason}"
                else:
                    reason = f"{best.engine_id} chosen for {step_kind} (skill score {best.skill_score})"

        eng_obj = self.store.get_engine(best.engine_id) or {}
        decision = RouterDecision(
            chosen_id=best.engine_id,
            runner=best.runner,
            model=best.model,
            effort=eng_obj.get("effort", "medium"),
            cost_tier=best.cost_tier,
            reason=reason,
            scores={s.engine_id: s.final_score for s in available[:5]},
            is_fallback=bool(current_engine and current_engine != best.engine_id),
        )

        self.store.record_router_decision(
            chosen=decision.chosen_id,
            reason=decision.reason,
            step_id=step_id,
            scores_json=json.dumps(decision.scores),
        )
        return decision

    def estimate_job_size(self, prompt: str, changed_files: list[str] | None = None, diff_content: str | None = None) -> str:
        """Estimate job size (small, medium, large, critical) from plan and diff."""
        if diff_content:
            lines = len(diff_content.splitlines())
            file_count = len(changed_files or [])
            if file_count <= 3 and lines <= 200:
                return SIZE_SMALL
            if file_count <= 8 and lines <= 800:
                return SIZE_MEDIUM
            return SIZE_LARGE

        # Heuristic from prompt length / keywords
        p_lower = prompt.lower()
        if any(w in p_lower for w in ("fix", "typo", "doc", "test", "label", "readme", "lint")):
            return SIZE_SMALL
        if any(w in p_lower for w in ("overhaul", "architect", "database", "security", "auth", "migration", "redesign")):
            return SIZE_LARGE
        return SIZE_MEDIUM

    def build_isolated_environment(self, engine_id: str) -> dict[str, str]:
        """Build process environment with isolated Anthropic keys without touching ~/.claude."""
        eng = self.store.get_engine(engine_id) or {}
        env: dict[str, str] = {}
        if eng.get("runner") == "claude-compatible":
            api_key = self.cred_store.get_secret(f"api_key_{engine_id}") or self.cred_store.get_secret(f"api_key_{eng.get('model')}")
            base_url = (
                eng.get("base_url")
                or self.store.get_setting(f"engine.{engine_id}.base_url")
                or DEFAULT_BASE_URLS.get(engine_id)
            )

            if api_key:
                env["ANTHROPIC_AUTH_TOKEN"] = api_key
            if base_url:
                env["ANTHROPIC_BASE_URL"] = base_url
            if eng.get("model"):
                env["ANTHROPIC_MODEL"] = eng["model"]
                env["ANTHROPIC_SMALL_FAST_MODEL"] = eng["model"]

        return env

    def estimate_gold_saved_tonight(self) -> dict[str, Any]:
        """Estimate Gold/Standard quota saved by jobs successfully run on Economy engines."""
        stats = self.store.get_engine_stats()
        economy_ids = {e["id"] for e in self.store.list_engines() if e.get("cost_tier") == TIER_ECONOMY}
        economy_successes = sum(s.get("successes", 0) for s in stats if s.get("engine") in economy_ids)

        # Estimate savings: ~15,000 tokens per job or ~$0.30 per job
        est_tokens_saved = economy_successes * 15000
        est_dollars_saved = round(economy_successes * 0.30, 2)

        return {
            "economy_jobs_done": economy_successes,
            "tokens_saved": est_tokens_saved,
            "cost_saved_usd": est_dollars_saved,
            "headline": f"${est_dollars_saved} gold saved tonight (estimate)" if economy_successes > 0 else "0 gold burned tonight",
        }


def test_engine_cli_version(runner: str, custom_cmd: list[str] | None = None) -> tuple[bool, str]:
    """Test engine CLI by running `<cli> --version` safely with a list of args."""
    cmd = custom_cmd or [runner, "--version"]
    if runner == "claude-compatible":
        cmd = ["claude", "--version"]
    try:
        rc, out, err, timed_out = run_subprocess_with_timeout(cmd, timeout_seconds=10)
        if rc == 0:
            version_str = (out or err).strip().splitlines()[0] if (out or err) else "OK"
            return True, f"Verified: {version_str}"
        return False, f"CLI exited {rc}: {(err or out).strip()[:100]}"
    except Exception as exc:
        return False, f"Failed to run CLI test: {exc}"
