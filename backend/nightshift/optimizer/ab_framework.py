"""A/B measurement and auto-disable framework for token optimizer tools (BLUEPRINT 5g)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nightshift.events import Event, EventBus
from nightshift.store import Store

DEFAULT_MIN_AB_SAMPLES = 3
AGY_FAST_MIN_TURN_REDUCTION = 0.15


@dataclass(frozen=True)
class ABDecision:
    """Deterministic A/B comparison outcome for one tool on a phase kind."""

    tool: str
    phase_kind: str
    action: str  # "keep_enabled" | "auto_disable" | "keep_measuring"
    reason: str
    control_runs: int
    treatment_runs: int
    control_avg_tokens: float
    treatment_avg_tokens: float
    tokens_saved_per_run: int
    control_avg_turns: float
    treatment_avg_turns: float
    control_avg_retries: float
    treatment_avg_retries: float
    control_success_rate: float
    treatment_success_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "phaseKind": self.phase_kind,
            "action": self.action,
            "reason": self.reason,
            "controlRuns": self.control_runs,
            "treatmentRuns": self.treatment_runs,
            "controlAvgTokens": round(self.control_avg_tokens, 1),
            "treatmentAvgTokens": round(self.treatment_avg_tokens, 1),
            "tokensSavedPerRun": self.tokens_saved_per_run,
            "controlAvgTurns": round(self.control_avg_turns, 2),
            "treatmentAvgTurns": round(self.treatment_avg_turns, 2),
            "controlAvgRetries": round(self.control_avg_retries, 2),
            "treatmentAvgRetries": round(self.treatment_avg_retries, 2),
            "controlSuccessRate": round(self.control_success_rate, 3),
            "treatmentSuccessRate": round(self.treatment_success_rate, 3),
        }


def evaluate_ab_samples(
    tool: str,
    phase_kind: str,
    control_rows: list[dict[str, Any]],
    treatment_rows: list[dict[str, Any]],
    *,
    min_samples: int = DEFAULT_MIN_AB_SAMPLES,
) -> ABDecision:
    """Compare treatment (tool ON) vs control (tool OFF) on the same phase kind."""
    c_n = len(control_rows)
    t_n = len(treatment_rows)

    def _avg_tokens(rows: list[dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        return sum(int(r.get("tokens_in", 0)) + int(r.get("tokens_out", 0)) for r in rows) / len(rows)

    def _avg_field(rows: list[dict[str, Any]], key: str) -> float:
        if not rows:
            return 0.0
        return sum(float(r.get(key, 0)) for r in rows) / len(rows)

    def _success_rate(rows: list[dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        return sum(1 for r in rows if bool(r.get("success", 0))) / len(rows)

    c_tok = _avg_tokens(control_rows)
    t_tok = _avg_tokens(treatment_rows)
    c_turns = _avg_field(control_rows, "turns")
    t_turns = _avg_field(treatment_rows, "turns")
    c_retries = _avg_field(control_rows, "retry_turns")
    t_retries = _avg_field(treatment_rows, "retry_turns")
    c_succ = _success_rate(control_rows)
    t_succ = _success_rate(treatment_rows)

    saved_per_run = max(0, int(round(c_tok - t_tok))) if (c_n > 0 and t_n > 0) else 0

    if c_n < min_samples or t_n < min_samples:
        return ABDecision(
            tool=tool,
            phase_kind=phase_kind,
            action="keep_measuring",
            reason=(
                f"Collecting A/B baseline for {tool} on {phase_kind} phases "
                f"({c_n}/{min_samples} off, {t_n}/{min_samples} on)."
            ),
            control_runs=c_n,
            treatment_runs=t_n,
            control_avg_tokens=c_tok,
            treatment_avg_tokens=t_tok,
            tokens_saved_per_run=saved_per_run,
            control_avg_turns=c_turns,
            treatment_avg_turns=t_turns,
            control_avg_retries=c_retries,
            treatment_avg_retries=t_retries,
            control_success_rate=c_succ,
            treatment_success_rate=t_succ,
        )

    # 1. Success rate regression -> auto-disable
    if t_succ < c_succ:
        return ABDecision(
            tool=tool,
            phase_kind=phase_kind,
            action="auto_disable",
            reason=(
                f"{tool} was switched off automatically: success rate on {phase_kind} phases "
                f"dropped from {int(round(c_succ * 100))}% to {int(round(t_succ * 100))}%."
            ),
            control_runs=c_n,
            treatment_runs=t_n,
            control_avg_tokens=c_tok,
            treatment_avg_tokens=t_tok,
            tokens_saved_per_run=0,
            control_avg_turns=c_turns,
            treatment_avg_turns=t_turns,
            control_avg_retries=c_retries,
            treatment_avg_retries=t_retries,
            control_success_rate=c_succ,
            treatment_success_rate=t_succ,
        )

    # 2. Retry turns rose -> auto-disable
    if t_retries > c_retries:
        return ABDecision(
            tool=tool,
            phase_kind=phase_kind,
            action="auto_disable",
            reason=(
                f"{tool} was switched off automatically: retry turns on {phase_kind} phases "
                f"rose from {c_retries:.1f} to {t_retries:.1f}."
            ),
            control_runs=c_n,
            treatment_runs=t_n,
            control_avg_tokens=c_tok,
            treatment_avg_tokens=t_tok,
            tokens_saved_per_run=0,
            control_avg_turns=c_turns,
            treatment_avg_turns=t_turns,
            control_avg_retries=c_retries,
            treatment_avg_retries=t_retries,
            control_success_rate=c_succ,
            treatment_success_rate=t_succ,
        )

    # 3. Net tokens did not drop -> auto-disable
    if t_tok >= c_tok:
        return ABDecision(
            tool=tool,
            phase_kind=phase_kind,
            action="auto_disable",
            reason=(
                f"{tool} was switched off automatically: it did not cut net tokens on {phase_kind} phases "
                f"({int(round(t_tok)):,} on vs {int(round(c_tok)):,} off, estimate)."
            ),
            control_runs=c_n,
            treatment_runs=t_n,
            control_avg_tokens=c_tok,
            treatment_avg_tokens=t_tok,
            tokens_saved_per_run=0,
            control_avg_turns=c_turns,
            treatment_avg_turns=t_turns,
            control_avg_retries=c_retries,
            treatment_avg_retries=t_retries,
            control_success_rate=c_succ,
            treatment_success_rate=t_succ,
        )

    # 4. agy-fast specific gate: turns must drop by at least 15% (BLUEPRINT 5g)
    if tool == "agy-fast" and c_turns > 0 and t_turns > c_turns * (1.0 - AGY_FAST_MIN_TURN_REDUCTION):
        reduction_pct = int(round((1.0 - (t_turns / c_turns)) * 100))
        return ABDecision(
            tool=tool,
            phase_kind=phase_kind,
            action="auto_disable",
            reason=(
                f"agy-fast was switched off automatically: Antigravity turns dropped by {reduction_pct}%, "
                "which is below the 15% threshold."
            ),
            control_runs=c_n,
            treatment_runs=t_n,
            control_avg_tokens=c_tok,
            treatment_avg_tokens=t_tok,
            tokens_saved_per_run=0,
            control_avg_turns=c_turns,
            treatment_avg_turns=t_turns,
            control_avg_retries=c_retries,
            treatment_avg_retries=t_retries,
            control_success_rate=c_succ,
            treatment_success_rate=t_succ,
        )

    return ABDecision(
        tool=tool,
        phase_kind=phase_kind,
        action="keep_enabled",
        reason=(
            f"{tool} cut ~{saved_per_run:,} tokens per {phase_kind} phase (estimate) "
            "with no increase in retries or failures."
        ),
        control_runs=c_n,
        treatment_runs=t_n,
        control_avg_tokens=c_tok,
        treatment_avg_tokens=t_tok,
        tokens_saved_per_run=saved_per_run,
        control_avg_turns=c_turns,
        treatment_avg_turns=t_turns,
        control_avg_retries=c_retries,
        treatment_avg_retries=t_retries,
        control_success_rate=c_succ,
        treatment_success_rate=t_succ,
    )


class ABFramework:
    """Persists A/B runs, evaluates decisions, auto-disables regressions, and estimates nightly savings."""

    def __init__(
        self,
        store: Store,
        events: EventBus | None = None,
        *,
        min_samples: int = DEFAULT_MIN_AB_SAMPLES,
    ) -> None:
        self._store = store
        self._events = events
        self._min_samples = min_samples

    def record_run(
        self,
        *,
        tool: str,
        enabled: bool,
        phase_kind: str = "default",
        engine: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        turns: int = 1,
        retry_turns: int = 0,
        success: bool = True,
        created_at: str | None = None,
    ) -> int:
        return self._store.record_token_ab_run(
            tool=tool,
            enabled=enabled,
            phase_kind=phase_kind,
            engine=engine,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            turns=turns,
            retry_turns=retry_turns,
            success=success,
            created_at=created_at,
        )

    def evaluate_tool(
        self,
        tool: str,
        phase_kind: str = "default",
        *,
        min_samples: int | None = None,
    ) -> ABDecision:
        rows = self._store.list_token_ab_runs(tool=tool, phase_kind=phase_kind)
        control = [r for r in rows if not bool(r.get("enabled", 0))]
        treatment = [r for r in rows if bool(r.get("enabled", 0))]
        return evaluate_ab_samples(
            tool,
            phase_kind,
            control,
            treatment,
            min_samples=min_samples if min_samples is not None else self._min_samples,
        )

    def evaluate_and_apply(
        self,
        tool: str,
        phase_kind: str = "default",
        *,
        min_samples: int | None = None,
    ) -> ABDecision:
        """Evaluate A/B samples and auto-disable the tool if it regresses."""
        decision = self.evaluate_tool(tool, phase_kind, min_samples=min_samples)
        if decision.action == "auto_disable":
            self._store.upsert_token_tool_state(
                tool,
                enabled=False,
                auto_disabled=True,
                warning=decision.reason,
            )
            if self._events is not None:
                self._events.emit(
                    Event(
                        kind="token_optimizer.auto_disabled",
                        plain_text=decision.reason,
                        level="warn",
                        payload={"tool": tool, "phase_kind": phase_kind},
                    )
                )
        return decision

    def summarize_tonight(self, *, since_iso: str | None = None) -> dict[str, Any]:
        """Compute 'tokens saved (estimate)' per tool and total for tonight."""
        today_iso = since_iso or datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        all_rows = self._store.list_token_ab_runs(limit=1000)
        tonight_rows = [r for r in all_rows if str(r.get("created_at", "")) >= today_iso]

        tools_seen = sorted({str(r["tool"]) for r in all_rows})
        per_tool: list[dict[str, Any]] = []
        total_saved = 0

        for tool in tools_seen:
            kinds = sorted({str(r.get("phase_kind", "default")) for r in all_rows if str(r["tool"]) == tool})
            tool_saved_tonight = 0
            best_decision: ABDecision | None = None
            for kind in kinds:
                decision = self.evaluate_tool(tool, kind)
                if best_decision is None or decision.action == "auto_disable":
                    best_decision = decision
                if decision.tokens_saved_per_run > 0:
                    tonight_on_runs = sum(
                        1
                        for r in tonight_rows
                        if str(r["tool"]) == tool
                        and str(r.get("phase_kind", "default")) == kind
                        and bool(r.get("enabled", 0))
                    )
                    tool_saved_tonight += tonight_on_runs * decision.tokens_saved_per_run

            total_saved += tool_saved_tonight
            state = self._store.get_token_tool_state(tool) or {}
            per_tool.append(
                {
                    "tool": tool,
                    "enabled": bool(state.get("enabled", 0)),
                    "autoDisabled": bool(state.get("auto_disabled", 0)),
                    "tokensSavedTonight": tool_saved_tonight,
                    "estimateLabel": f"~{tool_saved_tonight:,} tokens saved (estimate)",
                    "abAction": best_decision.action if best_decision else "keep_measuring",
                    "abReason": best_decision.reason if best_decision else "",
                    "warning": str(state.get("warning", "")),
                }
            )

        warnings: list[str] = []
        for st in self._store.list_token_tool_states():
            warn = str(st.get("warning") or "").strip()
            if warn:
                warnings.append(warn)

        return {
            "totalTokensSaved": total_saved,
            "estimateLabel": f"~{total_saved:,} tokens saved tonight (estimate)",
            "perTool": per_tool,
            "warnings": warnings,
            "isEstimate": True,
        }
