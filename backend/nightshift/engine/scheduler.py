"""Phase scheduler, implementer router, handoff manager, and ETA estimator.

Implements BLUEPRINT sections 5a, 5c, 5d, 5e, and 5h.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.vcs import GitGhClient
from nightshift.quota.ledger import QuotaLedger
from nightshift.store import Store

DEFAULT_PRIORITY_LIST = [
    {"position": 1, "engine": "agy", "model": "", "effort": "medium"},
    {"position": 2, "engine": "gemini", "model": "", "effort": "medium"},
    {"position": 3, "engine": "codex", "model": "", "effort": "medium"},
    {"position": 4, "engine": "claude", "model": "", "effort": "medium"},
]

FALLBACK_KIND_DURATIONS: dict[str, float] = {
    "implement": 1800.0,
    "fix_ci": 600.0,
    "review": 300.0,
    "release": 300.0,
}


@dataclass(frozen=True)
class EngineChoice:
    engine: str
    model: str
    effort: str
    reason: str
    is_switchback: bool = False


@dataclass(frozen=True)
class EtaSnapshot:
    seconds_remaining: float
    formatted: str
    is_estimate: bool


class SameErrorDetector:
    """Tracks error signatures per phase to rotate engines or park phases on repeated failures."""

    def __init__(self) -> None:
        self._history: dict[str, list[tuple[str, str]]] = {}  # phase_id -> [(engine, error_sig)]

    def record_error(self, phase_id: str, engine: str, error_text: str) -> tuple[bool, bool]:
        """Record an error and check if rotation or parking is needed.

        Returns (should_rotate_engine, should_park_phase).
        """
        # Create normalized error signature (first 3 lines or 150 chars)
        sig_lines = [l.strip() for l in error_text.strip().splitlines() if l.strip()][:3]
        sig = "\n".join(sig_lines)[:150].lower()

        if phase_id not in self._history:
            self._history[phase_id] = []
        hist = self._history[phase_id]
        hist.append((engine, sig))

        # Check: identical signature twice in a row from the same engine -> rotate
        if len(hist) >= 2 and hist[-1] == hist[-2]:
            should_rotate = True
        else:
            should_rotate = False

        # Check: identical signature across 3 distinct engines -> park
        engines_with_same_sig = {e for e, s in hist if s == sig}
        if len(engines_with_same_sig) >= 3:
            return True, True

        return should_rotate, False


class CircuitBreaker:
    """Trips when 3 phases are parked in a row, halting new phase starts."""

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = threshold
        self._consecutive_parked = 0
        self._tripped = False

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def consecutive_parked_count(self) -> int:
        return self._consecutive_parked

    def record_phase_result(self, parked: bool) -> bool:
        """Record phase result and return True if circuit breaker has tripped."""
        if parked:
            self._consecutive_parked += 1
            if self._consecutive_parked >= self._threshold:
                self._tripped = True
        else:
            self._consecutive_parked = 0
        return self._tripped

    def reset(self) -> None:
        self._consecutive_parked = 0
        self._tripped = False


class Scheduler:
    """Coordinates implementer selection, handoffs, fast switch-back, and duration ETAs."""

    def __init__(
        self,
        store: Store,
        quota_ledger: QuotaLedger,
        login_checker: LoginHealthChecker | None = None,
        max_implement_attempts: int = 4,
        max_ci_fix_rounds: int = 4,
        max_run_invocations: int = 60,
        max_run_hours: float = 12.0,
        aggressive_switchback: bool = False,
    ) -> None:
        self.store = store
        self.quota_ledger = quota_ledger
        self.login_checker = login_checker
        self.max_implement_attempts = max_implement_attempts
        self.max_ci_fix_rounds = max_ci_fix_rounds
        self.max_run_invocations = max_run_invocations
        self.max_run_hours = max_run_hours
        self.aggressive_switchback = aggressive_switchback

        self.same_error_detector = SameErrorDetector()
        self.circuit_breaker = CircuitBreaker()
        self.total_invocations_count = 0

    def record_invocation(self) -> int:
        """Increment and return total AI invocations in this run."""
        self.total_invocations_count += 1
        return self.total_invocations_count

    def has_run_budget_remaining(self) -> bool:
        """Return False once the per-run max AI invocation cap is reached."""
        return self.total_invocations_count < self.max_run_invocations

    def get_priority_list(self) -> list[dict[str, Any]]:
        db_list = self.store.get_priority_list()
        if db_list:
            return db_list
        return list(DEFAULT_PRIORITY_LIST)

    def is_engine_available(self, engine_name: str, model: str = "") -> bool:
        """Check if an engine is installed, logged in, within budget, and not cooling down."""
        if self.quota_ledger.is_cooling_down(engine_name, model):
            return False

        # Enforce owner-set budget caps per engine (BLUEPRINT 5d)
        for eng in self.store.list_engines():
            if eng.get("runner") == engine_name and (not model or eng.get("model") == model):
                if not eng.get("enabled", 1):
                    return False
                if not self.quota_ledger.check_budget_cap(engine_name, eng.get("budget_json", "{}")):
                    return False
                break

        if self.login_checker is not None:
            status = self.login_checker.check_tool(engine_name, in_run=True)
            if status.state in ("Logged out", "Not installed") or status.state.startswith("Limit reached"):
                return False

        return True

    def invocation_timeout_seconds(
        self,
        phase_id: str,
        chosen_engine: str,
        default_timeout_seconds: int = 3600,
    ) -> int:
        """Return invocation timeout, capping fallback invocations at 30 min under aggressive switch-back."""
        agg = self.aggressive_switchback or (self.store.get_setting("aggressive_switchback", "false").lower() == "true")
        if not agg:
            return default_timeout_seconds
        pref_engine, pref_model, _ = self.get_preferred_engine(phase_id)
        if chosen_engine != pref_engine and self.quota_ledger.is_cooling_down(pref_engine, pref_model):
            return min(default_timeout_seconds, 1800)
        return default_timeout_seconds

    def get_preferred_engine(self, phase_id: str, default_ladder: str = "agy") -> tuple[str, str, str]:
        """Return (engine, model, effort) preferred by owner override or plan."""
        assignment = self.store.get_phase_assignment(phase_id)
        if assignment:
            if assignment.get("override_engine"):
                return (
                    str(assignment["override_engine"]),
                    str(assignment.get("model") or ""),
                    str(assignment.get("effort") or "medium"),
                )
            if assignment.get("planned_engine"):
                return (
                    str(assignment["planned_engine"]),
                    str(assignment.get("model") or ""),
                    str(assignment.get("effort") or "medium"),
                )
        return (default_ladder, "", "medium")

    def select_engine(
        self,
        phase_id: str,
        step_kind: str = "implement",
        current_engine: str | None = None,
        exclude_engines: set[str] | None = None,
    ) -> EngineChoice | None:
        """Select the next engine according to BLUEPRINT 5c.

        Hierarchy:
          1. Preferred engine (switch-back if cooldown ended)
          2. Global priority list
          3. Router score
        """
        excluded = set(exclude_engines or set())
        preferred_engine, pref_model, pref_effort = self.get_preferred_engine(phase_id)

        # Fast switch-back check (BLUEPRINT 5c):
        # If current engine was a fallback and preferred engine is now available, switch back immediately!
        if preferred_engine not in excluded and self.is_engine_available(preferred_engine, pref_model):
            is_sb = bool(current_engine and current_engine != preferred_engine)
            reason = "Preferred engine available" + (" (fast switch-back)" if is_sb else "")
            return EngineChoice(
                engine=preferred_engine,
                model=pref_model,
                effort=pref_effort,
                reason=reason,
                is_switchback=is_sb,
            )

        # Fall back through priority list (Economy -> Standard -> Gold, Claude always last)
        priority_list = self.get_priority_list()
        for item in priority_list:
            eng = item["engine"]
            mod = item.get("model", "")
            eff = item.get("effort", "medium")
            if eng in excluded:
                continue
            if self.is_engine_available(eng, mod):
                reason = f"Priority list choice: {preferred_engine} unavailable or cooling down"
                return EngineChoice(
                    engine=eng,
                    model=mod,
                    effort=eff,
                    reason=reason,
                    is_switchback=False,
                )

        return None

    def create_mid_phase_handoff(
        self,
        vcs: GitGhClient,
        worktree_dir: Path,
        phase_id: str,
        phase_title: str,
        from_engine: str,
        to_engine: str,
        reason: str,
        remaining_acceptance_tests: str = "",
        last_error: str = "",
        branch: str | None = None,
    ) -> str | None:
        """Create WIP checkpoint, push to branch, write handoff note, and record in store (BLUEPRINT 5c)."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        commit_msg = f"nightshift: checkpoint {phase_id} {from_engine} {now_str}"

        # 1. Inspect diff before checkpoint
        stat = vcs.get_diff(worktree_dir)
        stat_tail = stat[-1500:] if stat else "No unstaged changes"

        # 2. Checkpoint commit and push
        target_branch = branch or phase_id
        checkpoint_sha = vcs.create_checkpoint_commit_and_push(worktree_dir, target_branch, commit_msg)
        sha_str = checkpoint_sha or "uncommitted"

        # 3. Write .nightshift/handoff.md
        handoff_dir = worktree_dir / ".nightshift"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        handoff_file = handoff_dir / "handoff.md"
        content = (
            f"# NightShift Handoff Note\n\n"
            f"- **Phase**: {phase_title} (`{phase_id}`)\n"
            f"- **From Engine**: {from_engine}\n"
            f"- **To Engine**: {to_engine}\n"
            f"- **Reason**: {reason}\n"
            f"- **Timestamp**: {now_str}\n"
            f"- **Checkpoint SHA**: `{sha_str}`\n\n"
            f"## Work Completed Tail\n```\n{stat_tail}\n```\n\n"
            f"## Remaining Requirements / Tests\n{remaining_acceptance_tests or 'Verify all phase requirements.'}\n\n"
            f"## Last Error / Context\n```\n{last_error or 'None recorded.'}\n```\n"
        )
        handoff_file.write_text(content, encoding="utf-8")

        # 4. Record handoff in SQLite
        self.store.record_handoff(
            phase_id=phase_id,
            from_engine=from_engine,
            to_engine=to_engine,
            reason=reason,
            checkpoint_sha=sha_str,
        )

        return sha_str

    def calculate_eta(
        self,
        pending_phase_count: int,
        active_step_kind: str = "implement",
        active_runner: str = "agy",
    ) -> EtaSnapshot:
        """Estimate remaining runtime based on median durations per (kind, runner)."""
        durations = self.store.get_durations(kind=active_step_kind, runner=active_runner)
        is_measured = len(durations) >= 3

        if durations:
            step_duration = statistics.median(durations)
        else:
            step_duration = FALLBACK_KIND_DURATIONS.get(active_step_kind, 1800.0)

        # Average phase estimated duration (implement + possible fix + review/release)
        phase_durations = self.store.get_durations()
        if len(phase_durations) >= 3:
            avg_phase = statistics.median(phase_durations)
            is_measured = True
        else:
            avg_phase = 2400.0

        total_sec = step_duration + (pending_phase_count * avg_phase)
        hours = int(total_sec // 3600)
        minutes = int((total_sec % 3600) // 60)

        estimate_suffix = " (estimate from past runs)" if is_measured else " (estimate)"
        if hours > 0:
            formatted = f"~{hours} h {minutes} m left{estimate_suffix}"
        else:
            formatted = f"~{minutes} m left{estimate_suffix}"

        return EtaSnapshot(
            seconds_remaining=total_sec,
            formatted=formatted,
            is_estimate=not is_measured,
        )
