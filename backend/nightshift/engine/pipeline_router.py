"""Cheap triage role, economics ledger, benchmark graduation, hard budgets, and risk-based router.

Implements Governance Amendments C, I, J and docs/AGENT_PIPELINE.md sections 2 (Stages 4, 9), 6.2, 6.3, 7.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from nightshift.engine.pipeline_review import engine_provider
from nightshift.store import Store

MIN_ACCEPTANCE_THRESHOLD: dict[str, float] = {
    "docs_format": 0.80,
    "ui_cosmetic": 0.80,
    "business_logic": 0.85,
    "high_risk": 0.90,
    "governance": 0.90,
}

# Stage maturity labels (Amendment J: Implemented | Integration-tested | Production-proven)
PIPELINE_STAGE_MATURITY: list[dict[str, str]] = [
    {"stage": "PLAN", "role": "planner", "maturity": "Integration-tested", "description": "Frozen contracts and acceptance specs"},
    {"stage": "CODE", "role": "coder", "maturity": "Integration-tested", "description": "Risk-routed coding in isolated worktree"},
    {"stage": "MECHANICAL", "role": "mechanical", "maturity": "Integration-tested", "description": "Deterministic build, tests, screenshots, PR body"},
    {"stage": "TRIAGE", "role": "triage", "maturity": "Integration-tested", "description": "Compact <=10-line CI failure summary"},
    {"stage": "FIX", "role": "fixer", "maturity": "Integration-tested", "description": "Risk-routed targeted fix commits"},
    {"stage": "REVIEW", "role": "reviewer", "maturity": "Integration-tested", "description": "Cross-provider raw-diff review with validated citations"},
    {"stage": "GATES", "role": "gatekeeper", "maturity": "Integration-tested", "description": "Exact-SHA CI, real duration, artifacts, protected tests"},
    {"stage": "MERGE", "role": "releaser", "maturity": "Integration-tested", "description": "MergeGate chokepoint and outbox reconciliation"},
    {"stage": "RELEASE", "role": "releaser", "maturity": "Integration-tested", "description": "Post-merge CI, mandatory smoke test, one-click rollback"},
]


def _utc_month_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


@dataclass(frozen=True)
class TriageSummary:
    root_cause: str
    file_location: str
    suggested_fix: str
    lines: list[str]

    def formatted(self) -> str:
        return "\n".join(self.lines[:10])


def summarize_ci_failure_triage(raw_log: str, *, max_lines: int = 10) -> TriageSummary:
    """Deterministic parser/formatter for Stage 4 TRIAGE output (enforcing <= 10 lines).

    Can wrap an AI triage response from ``small_job_ladder`` or fall back to plain-code log extraction.
    """
    lines_raw = [ln.strip() for ln in raw_log.splitlines() if ln.strip()]
    file_loc = "unknown:1"
    root_cause = "CI check failed"
    suggested_fix = "Inspect the failing test/build step and fix the root cause."

    file_re = re.compile(r"([A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|jsx|json|yml|yaml)):(\d+)")
    for ln in lines_raw:
        m = file_re.search(ln)
        if m:
            file_loc = f"{m.group(1).replace(chr(92), '/')}:{m.group(2)}"
            break

    for ln in lines_raw:
        low = ln.lower()
        if any(k in low for k in ("error:", "failed", "assertionerror", "exception", "typeerror", "syntaxerror")):
            root_cause = ln[:160]
            break

    for ln in lines_raw:
        if ln.lower().startswith(("fix:", "suggested fix:", "suggestion:")):
            suggested_fix = ln.split(":", 1)[1].strip()[:160]
            break

    compact = [
        f"Cause: {root_cause}",
        f"File: {file_loc}",
        f"Fix: {suggested_fix}",
    ]
    cap = min(max_lines, 10)
    return TriageSummary(
        root_cause=root_cause,
        file_location=file_loc,
        suggested_fix=suggested_fix,
        lines=compact[:cap],
    )


@dataclass(frozen=True)
class BudgetLimits:
    free_only_mode: bool = True
    max_task_usd_est: float = 0.0
    max_task_attempts: int = 4
    max_task_ci_fix_rounds: int = 4
    max_task_review_fix_rounds: int = 2
    max_task_wall_minutes: float = 90.0
    max_night_usd_est: float = 0.0
    max_night_invocations: int = 60
    max_night_hours: float = 8.0
    max_month_usd_est: float = 0.0


@dataclass(frozen=True)
class BudgetCheckResult:
    allowed: bool
    reason: str = ""
    scope: str = ""  # "task" | "night" | "month" | "free_only"


class BudgetGuard:
    """Enforces three nested hard budgets (task / night / month) + free-only default."""

    def __init__(self, store: Store, limits: BudgetLimits | None = None) -> None:
        self.store = store
        self.limits = limits or BudgetLimits()

    def monthly_spend_usd(self) -> float:
        month_start = f"{_utc_month_prefix()}-01T00:00:00"
        rows = self.store.list_pipeline_ledger(since_iso=month_start, limit=5000)
        return round(sum(float(r.get("est_token_cost_usd", 0.0)) for r in rows), 6)

    def task_spend_usd(self, task_id: str) -> float:
        rows = self.store.list_pipeline_ledger(task_id=task_id, limit=500)
        return round(sum(float(r.get("est_token_cost_usd", 0.0)) for r in rows), 6)

    def check_before_invocation(
        self,
        *,
        task_id: str,
        engine_is_paid: bool,
        estimated_call_usd: float = 0.0,
        task_attempts_used: int = 0,
        night_invocations_used: int = 0,
        night_spend_usd: float = 0.0,
        night_hours_elapsed: float = 0.0,
    ) -> BudgetCheckResult:
        lim = self.limits
        if lim.free_only_mode and (engine_is_paid or estimated_call_usd > 0.0):
            return BudgetCheckResult(
                False,
                "Free-only mode is enabled; paid engines are blocked until you opt in and set a monthly cap.",
                scope="free_only",
            )
        if task_attempts_used >= lim.max_task_attempts:
            return BudgetCheckResult(
                False,
                f"Task attempt budget exhausted ({task_attempts_used}/{lim.max_task_attempts}).",
                scope="task",
            )
        t_spend = self.task_spend_usd(task_id)
        if engine_is_paid and (t_spend + estimated_call_usd) > lim.max_task_usd_est:
            return BudgetCheckResult(
                False,
                f"Task budget exceeded (${t_spend + estimated_call_usd:.2f} > ${lim.max_task_usd_est:.2f} estimate).",
                scope="task",
            )
        if night_invocations_used >= lim.max_night_invocations:
            return BudgetCheckResult(
                False,
                f"Nightly invocation cap reached ({night_invocations_used}/{lim.max_night_invocations}).",
                scope="night",
            )
        if night_hours_elapsed >= lim.max_night_hours:
            return BudgetCheckResult(
                False,
                f"Nightly wall-clock cap reached ({night_hours_elapsed:.1f}h / {lim.max_night_hours:.1f}h).",
                scope="night",
            )
        if engine_is_paid and (night_spend_usd + estimated_call_usd) > lim.max_night_usd_est:
            return BudgetCheckResult(
                False,
                f"Nightly spend cap exceeded (${night_spend_usd + estimated_call_usd:.2f} > ${lim.max_night_usd_est:.2f} estimate).",
                scope="night",
            )
        m_spend = self.monthly_spend_usd()
        if engine_is_paid and (m_spend + estimated_call_usd) > lim.max_month_usd_est:
            return BudgetCheckResult(
                False,
                f"Monthly spend cap exceeded (${m_spend + estimated_call_usd:.2f} > ${lim.max_month_usd_est:.2f} estimate).",
                scope="month",
            )
        return BudgetCheckResult(True)


class PipelineEconomics:
    """Computes cost per accepted change, regression rates, and Economy benchmark graduation."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def recompute_model_score(
        self,
        *,
        engine_id: str,
        model: str,
        role: str,
        risk_class: str,
        incumbent_standard_cost_usd: float | None = None,
        is_economy: bool = False,
        ci_minute_cost_usd: float = 0.008,
        storage_mb_cost_usd: float = 0.0005,
    ) -> dict[str, Any]:
        """Aggregate ``pipeline_ledger`` rows into ``model_scores`` and evaluate graduation/demotion."""
        rows = [
            r
            for r in self.store.list_pipeline_ledger(limit=5000)
            if r["engine_id"] == engine_id
            and r["model"] == model
            and r["role"] == role
            and r["risk_class"] == risk_class
        ]
        # Group by task_id
        by_task: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_task.setdefault(str(r["task_id"]), []).append(r)

        sample_tasks = len(by_task)
        accepted_tasks = 0
        regression_count = 0
        total_attempts = len(rows)
        total_retries = 0
        total_wall_s = 0.0
        total_cost = 0.0

        for task_rows in by_task.values():
            outcomes = [str(x["outcome"]) for x in task_rows]
            if "accepted" in outcomes:
                accepted_tasks += 1
            if "reverted" in outcomes:
                regression_count += 1
            total_retries += sum(1 for o in outcomes if o == "retry")
            for x in task_rows:
                total_wall_s += float(x.get("wall_seconds", 0.0))
                total_cost += (
                    float(x.get("est_token_cost_usd", 0.0))
                    + float(x.get("ci_minutes", 0.0)) * ci_minute_cost_usd
                    + float(x.get("artifact_mb", 0.0)) * storage_mb_cost_usd
                )

        avg_retries = round(total_retries / sample_tasks, 3) if sample_tasks else 0.0
        avg_turnaround_min = round((total_wall_s / 60.0) / sample_tasks, 2) if sample_tasks else 0.0
        cost_per_accepted = round(total_cost / accepted_tasks, 4) if accepted_tasks else round(total_cost, 4)

        thresh = MIN_ACCEPTANCE_THRESHOLD.get(risk_class, 0.85)
        acceptance_rate = (accepted_tasks / sample_tasks) if sample_tasks else 0.0
        regression_rate = (regression_count / accepted_tasks) if accepted_tasks else 0.0

        # Check existing graduation state for rolling demotion rule (AGENT_PIPELINE §6.3)
        existing_scores = {
            (s["engine_id"], s["model"], s["role"], s["risk_class"]): bool(s["benchmark_graduated"])
            for s in self.store.list_model_scores(role=role, risk_class=risk_class)
        }
        was_graduated = existing_scores.get((engine_id, model, role, risk_class), False)

        graduated = was_graduated
        if is_economy:
            if not was_graduated:
                cheaper_than_incumbent = (
                    incumbent_standard_cost_usd is None or cost_per_accepted < incumbent_standard_cost_usd
                )
                if (
                    sample_tasks >= 10
                    and acceptance_rate >= thresh
                    and regression_rate <= 0.05
                    and avg_retries <= 1.5
                    and cheaper_than_incumbent
                ):
                    graduated = True
            else:
                # Rolling 10-task demotion check
                recent_task_ids = list(by_task.keys())[:10]
                if len(recent_task_ids) >= 10:
                    rec_acc = sum(1 for tid in recent_task_ids if any(x["outcome"] == "accepted" for x in by_task[tid]))
                    rec_reg = sum(1 for tid in recent_task_ids if any(x["outcome"] == "reverted" for x in by_task[tid]))
                    rec_acc_rate = rec_acc / len(recent_task_ids)
                    rec_reg_rate = (rec_reg / rec_acc) if rec_acc else 1.0
                    if rec_acc_rate < (thresh - 0.10) or rec_reg_rate > 0.10:
                        graduated = False
        else:
            graduated = True

        return self.store.upsert_model_score(
            engine_id=engine_id,
            model=model,
            role=role,
            risk_class=risk_class,
            sample_tasks=sample_tasks,
            accepted_tasks=accepted_tasks,
            total_attempts=total_attempts,
            regression_count=regression_count,
            avg_retries=avg_retries,
            avg_turnaround_min=avg_turnaround_min,
            total_cost_usd_est=round(total_cost, 4),
            cost_per_accepted_usd_est=cost_per_accepted,
            benchmark_graduated=graduated,
        )


@dataclass(frozen=True)
class RouterEngineSpec:
    engine_id: str
    model: str
    tier: str  # "economy" | "free_standard" | "standard" | "gold"
    enabled: bool = True
    available: bool = True
    is_paid: bool = False
    skill_score: float = 3.0
    priority_rank: int = 10


@dataclass(frozen=True)
class PipelineRouteDecision:
    chosen: RouterEngineSpec | None
    blocked: bool
    reason: str
    warning: str = ""


class PipelineRouter:
    """Safeguard-first, bootstrap-safe risk-based router (AGENT_PIPELINE §6.2)."""

    def __init__(self, store: Store, budget_guard: BudgetGuard | None = None) -> None:
        self.store = store
        self.budget_guard = budget_guard or BudgetGuard(store)

    def route(
        self,
        *,
        task_id: str,
        role: str,
        risk_class: str,
        engines: list[RouterEngineSpec],
        contributing_providers: list[str] | set[str] = (),
        owner_override: str | None = None,
        free_only_mode: bool | None = None,
    ) -> PipelineRouteDecision:
        """Select the cheapest reliable eligible engine, enforcing hard safeguards BEFORE owner overrides."""
        free_only = self.budget_guard.limits.free_only_mode if free_only_mode is None else free_only_mode
        excluded_providers = {p.strip().lower() for p in contributing_providers if p}
        thresh = MIN_ACCEPTANCE_THRESHOLD.get(risk_class, 0.85)

        scores_by_key = {
            (s["engine_id"], s["model"]): s
            for s in self.store.list_model_scores(role=role, risk_class=risk_class)
        }

        def check_hard_eligibility(eng: RouterEngineSpec) -> tuple[bool, str]:
            if not eng.enabled:
                return False, f"{eng.engine_id} is disabled"
            if not eng.available:
                return False, f"{eng.engine_id} is unavailable or cooling down"
            if free_only and (eng.is_paid or eng.tier not in ("economy", "free_standard")):
                return False, f"free_only_mode blocks paid/non-free engine {eng.engine_id}"
            bcheck = self.budget_guard.check_before_invocation(
                task_id=task_id,
                engine_is_paid=eng.is_paid,
            )
            if not bcheck.allowed:
                return False, bcheck.reason
            if role == "reviewer" and engine_provider(eng.engine_id) in excluded_providers:
                return False, f"provider '{engine_provider(eng.engine_id)}' is in contributing_providers"
            if risk_class in ("high_risk", "governance") and eng.tier not in ("standard", "free_standard", "gold"):
                return False, f"risk_class '{risk_class}' requires Standard/Gold tier (got '{eng.tier}')"
            if eng.tier == "economy":
                sc = scores_by_key.get((eng.engine_id, eng.model))
                if not sc or not bool(sc.get("benchmark_graduated")):
                    return False, f"Economy engine {eng.engine_id} is not benchmark_graduated for ({role}, {risk_class})"
            return True, ""

        eligible: list[RouterEngineSpec] = []
        rejection_reasons: dict[str, str] = {}
        by_id = {e.engine_id: e for e in engines}

        for eng in engines:
            ok, why = check_hard_eligibility(eng)
            if ok:
                eligible.append(eng)
            else:
                rejection_reasons[eng.engine_id] = why

        # Step 2: Owner override check ONLY against hard-eligible candidates (P1 fix #1)
        if owner_override:
            if owner_override in {e.engine_id for e in eligible}:
                chosen = by_id[owner_override]
                return PipelineRouteDecision(
                    chosen=chosen,
                    blocked=False,
                    reason=f"Owner pinned {chosen.engine_id} (verified eligible).",
                )
            fail_reason = rejection_reasons.get(owner_override, f"unknown engine '{owner_override}'")
            return PipelineRouteDecision(
                chosen=None,
                blocked=True,
                reason=f"Owner override {owner_override} rejected: {fail_reason}",
            )

        if not eligible:
            summary = "; ".join(f"{k}: {v}" for k, v in rejection_reasons.items())
            return PipelineRouteDecision(
                chosen=None,
                blocked=True,
                reason=f"No eligible engine available ({summary}).",
            )

        # Step 4: Bootstrap-safe effective success rate (P2 fix #13)
        scored: list[tuple[RouterEngineSpec, float, float, float]] = []
        for eng in eligible:
            sc = scores_by_key.get((eng.engine_id, eng.model))
            samples = int(sc["sample_tasks"]) if sc else 0
            if samples >= 5 and sc is not None:
                eff_rate = int(sc["accepted_tasks"]) / samples
                reg_rate = (int(sc["regression_count"]) / int(sc["accepted_tasks"])) if int(sc["accepted_tasks"]) else 1.0
                cost_acc = float(sc["cost_per_accepted_usd_est"])
            else:
                eff_rate = thresh  # neutral bootstrap prior so cold start never deadlocks
                reg_rate = 0.0
                cost_acc = (
                    0.01
                    if eng.tier == "economy"
                    else (0.05 if eng.tier == "free_standard" else (0.25 if eng.tier == "standard" else 1.0))
                )
            scored.append((eng, eff_rate, reg_rate, cost_acc))

        qualifying = [item for item in scored if item[1] >= thresh]
        if qualifying:
            qualifying.sort(key=lambda item: (item[3], item[2], -item[0].skill_score, item[0].priority_rank))
            best = qualifying[0]
            return PipelineRouteDecision(
                chosen=best[0],
                blocked=False,
                reason=(
                    f"Chose {best[0].engine_id} ({best[0].model}) for {role}/{risk_class}: "
                    f"lowest est. cost ${best[3]:.3f}/accepted at {best[1] * 100:.0f}% rate."
                ),
            )

        # Step 5: Fallback when all measured engines fell below threshold
        if risk_class in ("high_risk", "governance"):
            return PipelineRouteDecision(
                chosen=None,
                blocked=True,
                reason=f"No Standard/Gold engine meets reliability threshold ({thresh * 100:.0f}%) for {risk_class}.",
            )

        strong_fallback = [item for item in scored if item[0].tier in ("standard", "free_standard", "gold")]
        if not strong_fallback:
            strong_fallback = scored
        strong_fallback.sort(key=lambda item: (-item[1], item[2], -item[0].skill_score, item[0].priority_rank))
        best = strong_fallback[0]
        return PipelineRouteDecision(
            chosen=best[0],
            blocked=False,
            reason=f"Fallback to highest-reliability engine {best[0].engine_id} ({best[1] * 100:.0f}% < {thresh * 100:.0f}%).",
            warning=f"All measured engines fell below {thresh * 100:.0f}% threshold for {risk_class}.",
        )
