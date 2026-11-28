"""Pipeline economics ledger, benchmark graduation, hard budgets, and maturity labels.

Implements Governance Amendments I & J and docs/AGENT_PIPELINE.md sections 1, 6.2, 6.3, and 7.1
(Phase 8f: Cheap triage, economics ledger & model routing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nightshift.engine.pipeline_contracts import provider_for_engine
from nightshift.engine.pipeline_review import tier_for_engine
from nightshift.events import Event, EventBus
from nightshift.store import Store

# Reliability thresholds per risk class (AGENT_PIPELINE §6.2)
RISK_RELIABILITY_THRESHOLDS: dict[str, float] = {
    "docs_format": 0.80,
    "ui_cosmetic": 0.80,
    "business_logic": 0.85,
    "high_risk": 0.90,
    "governance": 0.90,
}

# Benchmark graduation constants (AGENT_PIPELINE §6.3)
GRADUATION_MIN_SAMPLES: int = 10
GRADUATION_MAX_REGRESSION_RATE: float = 0.05
GRADUATION_MAX_AVG_RETRIES: float = 1.5
DEMOTION_ACCEPTANCE_DROP_MARGIN: float = 0.10
DEMOTION_MAX_REGRESSION_RATE: float = 0.10
DEMOTION_ROLLING_WINDOW: int = 10

# Cost estimation rates (all explicitly marked as estimates, is_estimate=1)
CI_MINUTE_COST_USD_EST: float = 0.008
ARTIFACT_MB_STORAGE_USD_EST: float = 0.0005
DEFAULT_INCUMBENT_STANDARD_COST_USD_EST: float = 0.35

# Approximate token cost per 1M tokens (input, output) for cost-per-accepted-change estimates
EST_TOKEN_RATES_PER_MILLION: dict[str, tuple[float, float]] = {
    "agy": (0.0, 0.0),
    "gemini": (0.15, 0.60),
    "codex": (1.10, 4.40),
    "claude": (3.00, 15.00),
    "qwen": (0.0, 0.0),
    "deepseek": (0.14, 0.28),
    "kimi": (0.20, 0.60),
    "glm": (0.15, 0.45),
    "minimax": (0.15, 0.45),
}

# Engines that do NOT incur paid API spend when free_only_mode is ON (AGENT_PIPELINE §6.2, §7.1)
FREE_ONLY_ALLOWED_ENGINES: frozenset[str] = frozenset(
    {
        "qwen",
        "agy",
        "antigravity",
        "gemini",
        "codex",
    }
)

# 9 pipeline stages (AGENT_PIPELINE §2)
PIPELINE_STAGES: tuple[dict[str, str], ...] = (
    {"id": "PLAN", "label": "Plan", "role": "planner", "executor": "AI"},
    {"id": "CODE", "label": "Code", "role": "coder", "executor": "AI"},
    {"id": "MECHANICAL", "label": "Mechanical", "role": "mechanical", "executor": "Plain code"},
    {"id": "TRIAGE", "label": "Triage", "role": "triage", "executor": "AI"},
    {"id": "FIX", "label": "Fix", "role": "fixer", "executor": "AI"},
    {"id": "REVIEW", "label": "Review", "role": "reviewer", "executor": "AI"},
    {"id": "GATES", "label": "Gates", "role": "gatekeeper", "executor": "Plain code"},
    {"id": "MERGE", "label": "Merge", "role": "releaser", "executor": "Plain code"},
    {"id": "RELEASE", "label": "Release", "role": "releaser", "executor": "Plain code"},
)

PRODUCTION_PROVEN_MIN_ACCEPTED: int = 5


def min_threshold_for_risk(risk_class: str) -> float:
    """Return the minimum acceptance-rate threshold for ``risk_class``."""
    return RISK_RELIABILITY_THRESHOLDS.get(str(risk_class or "").strip().lower(), 0.85)


def is_free_only_engine(engine_id: str, runner: str = "", cost_tier: str = "") -> bool:
    """Return True if the engine incurs zero paid API spend (allowed under ``free_only_mode``)."""
    tier = (cost_tier or tier_for_engine(engine_id)).strip().lower()
    if tier == "gold":
        return False
    norm_runner = (runner or "").strip().lower()
    if norm_runner == "claude-compatible":
        return False
    norm_id = (engine_id or "").strip().lower()
    if norm_id in FREE_ONLY_ALLOWED_ENGINES or norm_runner in FREE_ONLY_ALLOWED_ENGINES:
        return True
    for prefix in FREE_ONLY_ALLOWED_ENGINES:
        if norm_id.startswith(f"{prefix}-") or norm_id.startswith(f"{prefix}_"):
            return True
    return False


def estimate_token_cost_usd(engine_id: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate token cost in USD (always an estimate)."""
    base = (engine_id or "").strip().lower().split("-")[0]
    in_rate, out_rate = EST_TOKEN_RATES_PER_MILLION.get(base, (0.50, 1.50))
    cost = (max(0, int(tokens_in)) / 1_000_000.0) * in_rate + (max(0, int(tokens_out)) / 1_000_000.0) * out_rate
    return round(cost, 6)


def compute_entry_total_cost_usd_est(entry: dict[str, Any]) -> float:
    """Total estimated USD cost for a single ``pipeline_ledger`` entry (tokens + CI + storage)."""
    token_cost = float(entry.get("est_token_cost_usd") or 0.0)
    ci_cost = float(entry.get("ci_minutes") or 0.0) * CI_MINUTE_COST_USD_EST
    storage_cost = float(entry.get("artifact_mb") or 0.0) * ARTIFACT_MB_STORAGE_USD_EST
    return round(token_cost + ci_cost + storage_cost, 6)


def compute_entry_paid_api_cost_usd_est(entry: dict[str, Any], store: Store | None = None) -> float:
    """Paid API USD spend for a single ``pipeline_ledger`` entry.

    Free-only engines (Qwen, Antigravity, Gemini CLI, Codex CLI) and non-AI delivery rows
    incur $0.00 paid API spend so their estimated economic cost / CI / storage never
    exhausts the zero-paid-spend cap under ``free_only_mode``.
    """
    role = str(entry.get("role") or "").strip().lower()
    engine_id = str(entry.get("engine_id") or "").strip()
    if role == "delivery" or not engine_id or engine_id.lower() in ("pipeline", "fallback"):
        return 0.0
    eng_row = store.get_engine(engine_id) if store is not None else None
    runner = str(eng_row.get("runner") or "") if eng_row else ""
    cost_tier = str(eng_row.get("cost_tier") or "") if eng_row else ""
    if is_free_only_engine(engine_id, runner=runner, cost_tier=cost_tier):
        return 0.0
    return round(max(0.0, float(entry.get("est_token_cost_usd") or 0.0)), 6)


@dataclass(frozen=True)
class BudgetCheckResult:
    """Result of checking per-task, per-night, and per-month hard budgets (AGENT_PIPELINE §7.1)."""

    allowed: bool
    scope: str = ""  # "" | "task" | "night" | "month"
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraduationDecision:
    """Result of evaluating Economy benchmark graduation / auto-demotion (§6.3)."""

    graduated: bool
    changed: bool
    status: str  # "graduated" | "benchmarking" | "demoted"
    reason: str


class PipelineEconomics:
    """Manages pipeline_ledger, model_scores, benchmark graduation, and hard budgets."""

    def __init__(self, store: Store, events: EventBus | None = None) -> None:
        self.store = store
        self.events = events

    def is_free_only_mode(self) -> bool:
        """Return True when free-only mode is active (default True per AGENT_PIPELINE §7.1)."""
        raw = self.store.get_setting("pipeline.freeOnlyMode", "1")
        return str(raw if raw is not None else "1").strip().lower() not in ("0", "false", "no", "off")

    def get_budget_caps(self) -> dict[str, float]:
        """Return configured night and month hard caps (defaults follow AGENT_PIPELINE §7.1)."""
        free_only = self.is_free_only_mode()
        default_usd = 0.0 if free_only else 5.0
        default_month_usd = 0.0 if free_only else 25.0

        def _float_setting(key: str, default: float) -> float:
            raw = self.store.get_setting(key)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return max(0.0, float(raw))
            except ValueError:
                return default

        return {
            "max_night_usd_est": 0.0 if free_only else _float_setting("budget.maxNightUsdEst", default_usd),
            "max_night_invocations": _float_setting("budget.maxNightInvocations", 60.0),
            "max_night_hours": _float_setting("budget.maxNightHours", 8.0),
            "max_month_usd_est": 0.0 if free_only else _float_setting("budget.maxMonthUsdEst", default_month_usd),
        }

    def record_ledger_event(
        self,
        *,
        task_id: str,
        role: str,
        engine_id: str,
        model: str,
        risk_class: str,
        outcome: str,
        attempt_id: int | None = None,
        provider: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        est_token_cost_usd: float | None = None,
        ci_minutes: float = 0.0,
        artifact_mb: float = 0.0,
        wall_seconds: float = 0.0,
        is_estimate: bool = True,
        created_at: str | None = None,
        recompute: bool = True,
    ) -> int:
        """Insert a ``pipeline_ledger`` row and optionally recompute ``model_scores``."""
        resolved_provider = provider or provider_for_engine(engine_id)
        token_cost = (
            float(est_token_cost_usd)
            if est_token_cost_usd is not None
            else estimate_token_cost_usd(engine_id, tokens_in, tokens_out)
        )
        row_id = self.store.record_pipeline_ledger(
            task_id=task_id,
            attempt_id=attempt_id,
            role=role,
            engine_id=engine_id,
            provider=resolved_provider,
            model=model,
            risk_class=risk_class,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            est_token_cost_usd=token_cost,
            ci_minutes=ci_minutes,
            artifact_mb=artifact_mb,
            wall_seconds=wall_seconds,
            outcome=outcome,
            is_estimate=is_estimate,
            created_at=created_at,
        )
        if recompute:
            self.recompute_model_score(engine_id=engine_id, model=model, role=role, risk_class=risk_class)
        return row_id

    def record_task_terminal_outcome(
        self,
        *,
        task_id: str,
        outcome: str,
        risk_class: str = "business_logic",
        ci_minutes: float = 0.0,
        artifact_mb: float = 0.0,
        wall_seconds: float = 0.0,
        created_at: str | None = None,
    ) -> int | None:
        """Record a terminal task delivery outcome (``accepted`` or ``reverted``) and refresh scores.

        Idempotent per ``(task_id, outcome)`` so crash recovery never double-counts deliveries.
        """
        norm_outcome = str(outcome or "").strip().lower()
        if norm_outcome not in ("accepted", "reverted"):
            raise ValueError(f"Unsupported terminal task outcome: {outcome!r}")

        existing_rows = self.store.list_pipeline_ledger(task_id=task_id, limit=500)
        already_recorded = any(
            str(r.get("role") or "").lower() == "delivery"
            and str(r.get("outcome") or "").lower() == norm_outcome
            for r in existing_rows
        )
        row_id: int | None = None
        if not already_recorded:
            row_id = self.store.record_pipeline_ledger(
                task_id=task_id,
                attempt_id=None,
                role="delivery",
                engine_id="pipeline",
                provider="pipeline",
                model="delivery",
                risk_class=risk_class,
                tokens_in=0,
                tokens_out=0,
                est_token_cost_usd=0.0,
                ci_minutes=ci_minutes,
                artifact_mb=artifact_mb,
                wall_seconds=wall_seconds,
                outcome=norm_outcome,
                is_estimate=True,
                created_at=created_at,
            )

        # Recompute model_scores for every AI participant on this task
        seen_keys: set[tuple[str, str, str, str]] = set()
        for r in existing_rows:
            r_role = str(r.get("role") or "").strip().lower()
            r_eng = str(r.get("engine_id") or "").strip()
            r_model = str(r.get("model") or "").strip()
            r_risk = str(r.get("risk_class") or risk_class).strip().lower()
            if not r_eng or r_role == "delivery" or r_eng.lower() in ("pipeline", "fallback"):
                continue
            key = (r_eng, r_model, r_role, r_risk)
            if key not in seen_keys:
                seen_keys.add(key)
                self.recompute_model_score(engine_id=r_eng, model=r_model, role=r_role, risk_class=r_risk)
        return row_id

    def _incumbent_standard_cost(self, role: str, risk_class: str) -> float:
        """Return the lowest ``cost_per_accepted_usd_est`` among measured Standard engines for (role, risk_class)."""
        scores = self.store.list_model_scores(role=role, risk_class=risk_class)
        standard_costs: list[float] = []
        for s in scores:
            eng = self.store.get_engine(str(s["engine_id"]))
            tier = tier_for_engine(str(s["engine_id"]), explicit_tier=eng.get("cost_tier") if eng else None)
            if tier == "standard" and int(s.get("accepted_tasks") or 0) > 0:
                standard_costs.append(float(s.get("cost_per_accepted_usd_est") or 0.0))
        if standard_costs:
            return min(standard_costs)
        return DEFAULT_INCUMBENT_STANDARD_COST_USD_EST

    def recompute_model_score(
        self,
        *,
        engine_id: str,
        model: str,
        role: str,
        risk_class: str,
    ) -> tuple[dict[str, Any], GraduationDecision]:
        """Aggregate ``pipeline_ledger`` into ``model_scores`` and apply §6.3 graduation/demotion."""
        rows = self.store.list_pipeline_ledger(
            engine_id=engine_id,
            model=model,
            role=role,
            risk_class=risk_class,
            limit=5000,
        )
        existing = self.store.get_model_score(engine_id, model, role, risk_class)
        prev_graduated = bool(existing.get("benchmark_graduated", 0)) if existing else False

        # Group rows by task_id preserving chronological order of task appearance
        tasks_order: list[str] = []
        by_task: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            tid = str(r["task_id"])
            if tid not in by_task:
                tasks_order.append(tid)
                by_task[tid] = []
            by_task[tid].append(r)

        sample_tasks = len(tasks_order)
        total_attempts = len(rows)
        accepted_tasks = 0
        regression_count = 0
        total_cost_usd_est = 0.0
        total_wall_seconds = 0.0
        task_outcomes: list[tuple[bool, bool]] = []  # (accepted, reverted)

        for tid in tasks_order:
            t_rows = by_task[tid]
            outcomes = {str(x.get("outcome") or "").lower() for x in t_rows}
            task_all_rows = self.store.list_pipeline_ledger(task_id=tid, limit=500)
            task_terminal_outcomes = {
                str(x.get("outcome") or "").lower()
                for x in task_all_rows
                if str(x.get("outcome") or "").lower() in ("accepted", "reverted")
            }
            all_outcomes = outcomes | task_terminal_outcomes
            reverted = "reverted" in all_outcomes
            accepted = ("accepted" in all_outcomes) and not reverted
            if accepted:
                accepted_tasks += 1
            if reverted:
                regression_count += 1
            task_outcomes.append((accepted, reverted))
            for x in t_rows:
                total_cost_usd_est += compute_entry_total_cost_usd_est(x)
                total_wall_seconds += float(x.get("wall_seconds") or 0.0)

        avg_retries = round(max(0.0, (total_attempts - sample_tasks) / sample_tasks), 3) if sample_tasks > 0 else 0.0
        avg_turnaround_min = round((total_wall_seconds / 60.0) / sample_tasks, 3) if sample_tasks > 0 else 0.0
        total_cost_usd_est = round(total_cost_usd_est, 6)
        cost_per_accepted_usd_est = (
            round(total_cost_usd_est / accepted_tasks, 6) if accepted_tasks > 0 else total_cost_usd_est
        )

        eng_row = self.store.get_engine(engine_id)
        tier = tier_for_engine(engine_id, explicit_tier=eng_row.get("cost_tier") if eng_row else None)
        thresh = min_threshold_for_risk(risk_class)

        if tier != "economy":
            # Standard and Gold engines do not require Economy benchmark graduation
            graduated = True
            decision = GraduationDecision(
                graduated=True,
                changed=False,
                status="graduated",
                reason=f"{tier.capitalize()} tier engine does not require Economy graduation.",
            )
        else:
            acceptance_rate = (accepted_tasks / sample_tasks) if sample_tasks > 0 else 0.0
            # Regression rate defined as regression_count / (accepted_tasks + regression_count) or accepted_tasks
            reg_denom = max(1, accepted_tasks + regression_count) if (accepted_tasks + regression_count) > 0 else 0
            regression_rate = (regression_count / reg_denom) if reg_denom > 0 else 0.0
            incumbent_cost = self._incumbent_standard_cost(role, risk_class)

            if prev_graduated:
                # Check rolling 10-task auto-demotion (AGENT_PIPELINE §6.3)
                window = task_outcomes[-DEMOTION_ROLLING_WINDOW:]
                win_len = len(window)
                win_accepted = sum(1 for acc, _ in window if acc)
                win_reverted = sum(1 for _, rev in window if rev)
                win_acc_rate = (win_accepted / win_len) if win_len > 0 else 1.0
                win_reg_denom = max(1, win_accepted + win_reverted) if (win_accepted + win_reverted) > 0 else 1
                win_reg_rate = win_reverted / win_reg_denom

                if win_len >= DEMOTION_ROLLING_WINDOW and (
                    win_acc_rate < (thresh - DEMOTION_ACCEPTANCE_DROP_MARGIN)
                    or win_reg_rate > DEMOTION_MAX_REGRESSION_RATE
                ):
                    graduated = False
                    reason = (
                        f"Auto-demoted {engine_id} ({model}) for {role}/{risk_class}: "
                        f"rolling 10-task acceptance {win_acc_rate:.0%} / regression {win_reg_rate:.0%}."
                    )
                    decision = GraduationDecision(graduated=False, changed=True, status="demoted", reason=reason)
                else:
                    graduated = True
                    decision = GraduationDecision(
                        graduated=True,
                        changed=False,
                        status="graduated",
                        reason="Graduated Economy model meets rolling reliability targets.",
                    )
            else:
                # Evaluate promotion criteria (AGENT_PIPELINE §6.3)
                meets_samples = sample_tasks >= GRADUATION_MIN_SAMPLES
                meets_acceptance = acceptance_rate >= thresh
                meets_regression = accepted_tasks > 0 and regression_rate <= GRADUATION_MAX_REGRESSION_RATE
                meets_retries = avg_retries <= GRADUATION_MAX_AVG_RETRIES
                meets_cost = cost_per_accepted_usd_est < incumbent_cost

                if meets_samples and meets_acceptance and meets_regression and meets_retries and meets_cost:
                    graduated = True
                    reason = (
                        f"Graduated {engine_id} ({model}) for {role}/{risk_class}: "
                        f"{accepted_tasks}/{sample_tasks} accepted ({acceptance_rate:.0%}), "
                        f"est. ${cost_per_accepted_usd_est:.3f}/accepted < Standard ${incumbent_cost:.3f}."
                    )
                    decision = GraduationDecision(graduated=True, changed=True, status="graduated", reason=reason)
                else:
                    graduated = False
                    missing: list[str] = []
                    if not meets_samples:
                        missing.append(f"{sample_tasks}/{GRADUATION_MIN_SAMPLES} sample tasks")
                    if not meets_acceptance:
                        missing.append(f"acceptance {acceptance_rate:.0%} < {thresh:.0%}")
                    if not meets_regression:
                        missing.append(f"regression {regression_rate:.0%} > {GRADUATION_MAX_REGRESSION_RATE:.0%}")
                    if not meets_retries:
                        missing.append(f"avg retries {avg_retries:.2f} > {GRADUATION_MAX_AVG_RETRIES}")
                    if not meets_cost:
                        missing.append(f"cost ${cost_per_accepted_usd_est:.3f} >= Standard ${incumbent_cost:.3f}")
                    decision = GraduationDecision(
                        graduated=False,
                        changed=False,
                        status="benchmarking",
                        reason=f"Benchmarking ({', '.join(missing)}).",
                    )

        score_row = self.store.upsert_model_score(
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
            total_cost_usd_est=total_cost_usd_est,
            cost_per_accepted_usd_est=cost_per_accepted_usd_est,
            benchmark_graduated=graduated,
        )

        if decision.changed and self.events is not None:
            kind = "pipeline.model_graduated" if decision.graduated else "pipeline.model_demoted"
            level = "info" if decision.graduated else "warn"
            self.events.emit(Event(kind=kind, plain_text=decision.reason, level=level))

        return score_row, decision

    def get_monthly_spend_usd_est(self, *, now: datetime | None = None) -> float:
        """Return total estimated economic USD cost in ``pipeline_ledger`` for the current UTC month."""
        ref = now or datetime.now(timezone.utc)
        month_start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = self.store.list_pipeline_ledger(since_iso=month_start, limit=10000)
        return round(sum(compute_entry_total_cost_usd_est(r) for r in rows), 6)

    def get_monthly_paid_spend_usd_est(self, *, now: datetime | None = None) -> float:
        """Return paid API USD spend in ``pipeline_ledger`` for the current UTC month."""
        ref = now or datetime.now(timezone.utc)
        month_start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = self.store.list_pipeline_ledger(since_iso=month_start, limit=10000)
        return round(sum(compute_entry_paid_api_cost_usd_est(r, self.store) for r in rows), 6)

    def get_tonight_spend_summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return tonight's paid API spend, total economic estimate, invocations, and wall hours."""
        ref = now or datetime.now(timezone.utc)
        day_start = ref.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = self.store.list_pipeline_ledger(since_iso=day_start, limit=10000)
        ai_rows = [r for r in rows if str(r.get("role") or "").lower() != "delivery"]
        paid_spent_usd = round(sum(compute_entry_paid_api_cost_usd_est(r, self.store) for r in rows), 6)
        economic_spent_usd = round(sum(compute_entry_total_cost_usd_est(r) for r in rows), 6)
        wall_hours = round(sum(float(r.get("wall_seconds") or 0.0) for r in ai_rows) / 3600.0, 4)
        caps = self.get_budget_caps()
        free_only = self.is_free_only_mode()
        if free_only:
            display_line = f"${paid_spent_usd:.2f} / $0.00 (free-only mode)"
        else:
            display_line = f"~${paid_spent_usd:.2f} / ${caps['max_night_usd_est']:.2f} tonight (estimate)"
        return {
            "freeOnlyMode": free_only,
            "spentUsdEst": paid_spent_usd,
            "paidSpentUsdEst": paid_spent_usd,
            "economicCostUsdEst": economic_spent_usd,
            "capUsdEst": caps["max_night_usd_est"],
            "invocations": len(ai_rows),
            "maxInvocations": int(caps["max_night_invocations"]),
            "wallHours": wall_hours,
            "maxHours": caps["max_night_hours"],
            "displayLine": display_line,
            "isEstimate": True,
        }

    def check_hard_budgets(
        self,
        *,
        task_id: str | None = None,
        role: str = "coder",
        engine_id: str = "",
        runner: str = "",
        cost_tier: str = "",
        projected_usd_est: float = 0.0,
        now: datetime | None = None,
    ) -> BudgetCheckResult:
        """Check all three nested hard budget caps (task, night, month) before launching an AI process."""
        free_only = self.is_free_only_mode()
        caps = self.get_budget_caps()
        is_free_engine = bool(engine_id) and is_free_only_engine(engine_id, runner=runner, cost_tier=cost_tier)
        effective_projected_paid_usd = 0.0 if is_free_engine else max(0.0, float(projected_usd_est))

        if free_only and engine_id and not is_free_engine:
            return BudgetCheckResult(
                allowed=False,
                scope="month",
                reason=f"Free-only mode is active; paid engine '{engine_id}' is blocked ($0.00 paid cap).",
                details={"free_only_mode": True, "engine_id": engine_id},
            )

        # 1. Per-task budget (from frozen contract)
        if task_id:
            task = self.store.get_task(task_id)
            if task is not None:
                contract = self.store.get_task_contract(str(task["contract_id"])) or {}
                try:
                    budget = json.loads(str(contract.get("budget_json") or "{}"))
                except Exception:
                    budget = {}
                max_tokens = int(budget.get("max_tokens", 120000))
                max_usd = 0.0 if free_only else float(budget.get("max_usd_est", 0.50))
                max_attempts = int(budget.get("max_attempts", 4))
                max_ci_fix = int(budget.get("max_ci_fix_rounds", 4))
                max_rev_fix = int(budget.get("max_review_fix_rounds", 2))
                max_wall_min = float(budget.get("max_wall_minutes", 90.0))

                norm_role = (role or "coder").strip().lower()
                if norm_role == "coder" and int(task.get("attempt_count") or 0) >= max_attempts:
                    return BudgetCheckResult(
                        allowed=False,
                        scope="task",
                        reason=f"Task {task_id} reached max code attempts ({task['attempt_count']}/{max_attempts}).",
                    )
                if norm_role == "fixer" and int(task.get("ci_fix_rounds") or 0) >= max_ci_fix:
                    return BudgetCheckResult(
                        allowed=False,
                        scope="task",
                        reason=f"Task {task_id} reached max CI-fix rounds ({task['ci_fix_rounds']}/{max_ci_fix}).",
                    )
                if norm_role == "review_fixer" and int(task.get("review_fix_rounds") or 0) >= max_rev_fix:
                    return BudgetCheckResult(
                        allowed=False,
                        scope="task",
                        reason=f"Task {task_id} reached max review-fix rounds ({task['review_fix_rounds']}/{max_rev_fix}).",
                    )

                task_rows = self.store.list_pipeline_ledger(task_id=task_id, limit=1000)
                used_tokens = sum(int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in task_rows)
                used_paid_usd = sum(compute_entry_paid_api_cost_usd_est(r, self.store) for r in task_rows)
                used_wall_min = sum(float(r.get("wall_seconds") or 0.0) for r in task_rows) / 60.0

                if max_tokens > 0 and used_tokens >= max_tokens:
                    return BudgetCheckResult(
                        allowed=False,
                        scope="task",
                        reason=f"Task {task_id} token budget exceeded ({used_tokens:,}/{max_tokens:,} tokens, estimate).",
                    )
                if (
                    not is_free_engine
                    and (used_paid_usd + effective_projected_paid_usd) > max_usd + 1e-9
                    and (used_paid_usd > 0.0 or effective_projected_paid_usd > 0.0)
                ):
                    return BudgetCheckResult(
                        allowed=False,
                        scope="task",
                        reason=f"Task {task_id} cost budget exceeded (${used_paid_usd + effective_projected_paid_usd:.2f} / ${max_usd:.2f} estimate).",
                    )
                if max_wall_min > 0 and used_wall_min >= max_wall_min:
                    return BudgetCheckResult(
                        allowed=False,
                        scope="task",
                        reason=f"Task {task_id} wall-clock budget exceeded ({used_wall_min:.1f}/{max_wall_min:.0f} min).",
                    )

        # 2. Per-night budget
        tonight = self.get_tonight_spend_summary(now=now)
        if tonight["invocations"] >= tonight["maxInvocations"]:
            return BudgetCheckResult(
                allowed=False,
                scope="night",
                reason=f"Nightly AI invocation cap reached ({tonight['invocations']}/{tonight['maxInvocations']}).",
            )
        if tonight["wallHours"] >= tonight["maxHours"]:
            return BudgetCheckResult(
                allowed=False,
                scope="night",
                reason=f"Nightly wall-clock cap reached ({tonight['wallHours']:.1f}/{tonight['maxHours']:.1f} hours).",
            )
        if (
            not is_free_engine
            and (tonight["paidSpentUsdEst"] + effective_projected_paid_usd) > caps["max_night_usd_est"] + 1e-9
            and (tonight["paidSpentUsdEst"] > 0.0 or effective_projected_paid_usd > 0.0)
        ):
            return BudgetCheckResult(
                allowed=False,
                scope="night",
                reason=(
                    f"Nightly cost cap reached (${tonight['paidSpentUsdEst'] + effective_projected_paid_usd:.2f} "
                    f"/ ${caps['max_night_usd_est']:.2f} estimate)."
                ),
            )

        # 3. Per-month budget
        monthly_paid_usd = self.get_monthly_paid_spend_usd_est(now=now)
        if (
            not is_free_engine
            and (monthly_paid_usd + effective_projected_paid_usd) > caps["max_month_usd_est"] + 1e-9
            and (monthly_paid_usd > 0.0 or effective_projected_paid_usd > 0.0)
        ):
            return BudgetCheckResult(
                allowed=False,
                scope="month",
                reason=(
                    f"Monthly cost cap reached (${monthly_paid_usd + effective_projected_paid_usd:.2f} "
                    f"/ ${caps['max_month_usd_est']:.2f} estimate). Paid engines locked until next UTC month."
                ),
            )

        return BudgetCheckResult(allowed=True)

    def get_stage_maturity(self) -> list[dict[str, Any]]:
        """Return Amendment J maturity labels for all 9 pipeline stages."""
        all_rows = self.store.list_pipeline_ledger(limit=10000)
        accepted_task_ids = {
            str(r["task_id"])
            for r in all_rows
            if str(r.get("outcome") or "").lower() == "accepted"
        }
        reverted_task_ids = {
            str(r["task_id"])
            for r in all_rows
            if str(r.get("outcome") or "").lower() == "reverted"
        }
        clean_accepted = len(accepted_task_ids - reverted_task_ids)
        label = "Production-proven" if clean_accepted >= PRODUCTION_PROVEN_MIN_ACCEPTED else "Integration-tested"
        evidence = (
            f"{clean_accepted} accepted overnight tasks recorded in ledger."
            if label == "Production-proven"
            else f"Verified with fake runners, fake gh, and Playwright MockBridge ({clean_accepted}/{PRODUCTION_PROVEN_MIN_ACCEPTED} real tasks for Production-proven)."
        )
        return [
            {
                "stage": st["id"],
                "label": st["label"],
                "role": st["role"],
                "executor": st["executor"],
                "maturity": label,
                "evidence": evidence,
            }
            for st in PIPELINE_STAGES
        ]

    def get_30d_summary(self, *, project_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Compute top-level 30-day KPI summary for the Pipeline Ledger & Scores UI (§11.2)."""
        ref = now or datetime.now(timezone.utc)
        since_30d = (ref - timedelta(days=30)).isoformat()
        rows = self.store.list_pipeline_ledger(since_iso=since_30d, project_id=project_id, limit=10000)

        by_task: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_task.setdefault(str(r["task_id"]), []).append(r)

        accepted_tasks = 0
        reverted_tasks = 0
        gold_saved_tasks = 0
        total_cost_usd_est = 0.0

        for tid, t_rows in by_task.items():
            outcomes = {str(x.get("outcome") or "").lower() for x in t_rows}
            reverted = "reverted" in outcomes
            accepted = ("accepted" in outcomes) and not reverted
            if accepted:
                accepted_tasks += 1
                # Count tasks accepted with a graduated Economy coder/fixer
                used_economy = any(
                    tier_for_engine(str(x.get("engine_id") or "")) == "economy"
                    and str(x.get("role") or "") in ("coder", "fixer", "triage")
                    for x in t_rows
                )
                if used_economy:
                    gold_saved_tasks += 1
            if reverted:
                reverted_tasks += 1
            for x in t_rows:
                total_cost_usd_est += compute_entry_total_cost_usd_est(x)

        cost_per_accepted = round(total_cost_usd_est / accepted_tasks, 4) if accepted_tasks > 0 else 0.0
        reg_denom = accepted_tasks + reverted_tasks
        regression_rate_pct = round((reverted_tasks / reg_denom) * 100.0, 1) if reg_denom > 0 else 0.0

        caps = self.get_budget_caps()
        monthly_paid_usd = self.get_monthly_paid_spend_usd_est(now=ref)
        monthly_economic_usd = self.get_monthly_spend_usd_est(now=ref)
        tonight = self.get_tonight_spend_summary(now=ref)

        return {
            "acceptedChanges30d": accepted_tasks,
            "costPerAcceptedUsdEst": cost_per_accepted,
            "regressionRatePct": regression_rate_pct,
            "goldSavedTasks": gold_saved_tasks,
            "freeOnlyMode": self.is_free_only_mode(),
            "monthlyBudget": {
                "spentUsdEst": monthly_paid_usd,
                "economicCostUsdEst": monthly_economic_usd,
                "capUsdEst": caps["max_month_usd_est"],
                "monthLabel": ref.strftime("%Y-%m UTC"),
                "isEstimate": True,
            },
            "tonightBudget": tonight,
            "isEstimate": True,
        }
