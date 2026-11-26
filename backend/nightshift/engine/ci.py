"""GitHub Actions CI watcher, flaky-test retry, engine rotation, and self-healing fix loop (BLUEPRINT 5d)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nightshift.engine.scheduler import Scheduler
from nightshift.engine.vcs import GitGhClient
from nightshift.engine.verify import RiskAssessment
from nightshift.runners.base import Runner, RunnerResult
from nightshift.store import Store


@dataclass(frozen=True)
class ReviewFinding:
    priority: str  # "P1" or "P2"
    message: str
    file_path: str | None = None


@dataclass(frozen=True)
class CiWatchResult:
    passed: bool
    flaky: bool = False
    rounds_used: int = 0
    detail: str = ""
    last_engine: str = ""
    review_findings: list[ReviewFinding] = field(default_factory=list)
    checked_sha: str | None = None


def parse_review_findings(text: str) -> list[ReviewFinding]:
    """Parse P1 and P2 findings from independent AI reviewer output."""
    findings: list[ReviewFinding] = []
    lines = text.splitlines()

    for line in lines:
        stripped = line.strip()
        # Match P1 patterns
        if re.search(r"\b(?:P1|PRIORITY[ -]?1|BLOCKING|CRITICAL|BUG|SECURITY)\b", stripped, re.IGNORECASE):
            findings.append(ReviewFinding(priority="P1", message=stripped))
        elif re.search(r"\b(?:P2|PRIORITY[ -]?2|NIT|SUGGESTION|INFO)\b", stripped, re.IGNORECASE):
            findings.append(ReviewFinding(priority="P2", message=stripped))

    return findings


class CiPipelineManager:
    """Manages CI watching, flaky detection, auto-fix rounds, engine rotation, and risk-based review."""

    def __init__(
        self,
        vcs: GitGhClient,
        store: Store,
        scheduler: Scheduler,
        runners: dict[str, Runner],
    ) -> None:
        self.vcs = vcs
        self.store = store
        self.scheduler = scheduler
        self.runners = runners

    def select_reviewer(self, implementer_engine: str) -> tuple[str, Runner] | tuple[None, None]:
        """Select independent reviewer per BLUEPRINT 5d.

        Order: Codex first -> Gemini CLI -> Economy engines (agy).
        Must be a different engine from the implementer when possible.
        """
        candidates = ["codex", "gemini", "agy"]
        # Filter out the engine that just implemented if another is available
        preferred_candidates = [c for c in candidates if c != implementer_engine] + [implementer_engine]

        for cand in preferred_candidates:
            if cand in self.runners and self.scheduler.is_engine_available(cand):
                return cand, self.runners[cand]
        return None, None

    async def watch_and_heal(
        self,
        repo: str,
        local_repo: Path,
        worktree_dir: Path,
        phase_id: str,
        phase_title: str,
        branch: str,
        pr_number: int,
        initial_engine: str,
        risk: RiskAssessment,
        *,
        max_retries: int = 4,
        guardrail_header: str = "",
        on_status_change: Callable[[str], None] | None = None,
    ) -> CiWatchResult:
        """Watch CI checks and auto-fix failures up to max_retries with engine rotation."""
        active_engine = initial_engine
        engine_failure_count = 0
        flaky_detected = False

        for round_num in range(max_retries + 1):
            if on_status_change:
                on_status_change(f"WATCHING_CI ({round_num + 1}/{max_retries + 1})")

            # Bind the check and review to a stable PR head.
            snapshot = self.vcs.find_open_pr(repo, branch)
            checked_sha = str(snapshot.get("headRefOid") or "") if snapshot else ""
            passed, ci_msg = self.vcs.watch_pr_checks(repo, pr_number, worktree_dir)

            # 1. Flaky test detector: if failed, re-run checks once before burning a fix round
            if not passed and round_num == 0:
                rerun_passed, rerun_msg = self.vcs.watch_pr_checks(repo, pr_number, worktree_dir)
                if rerun_passed:
                    flaky_detected = True
                    passed = True
                    ci_msg = "Passed on re-run without code change (flaky test)"

            if passed:
                # CI is green! Now run Risk-based review loop (BLUEPRINT 5d)
                review_findings: list[ReviewFinding] = []
                rev_engine_name, reviewer = self.select_reviewer(active_engine)
                if reviewer and rev_engine_name:
                    if on_status_change:
                        on_status_change(f"REVIEWING ({rev_engine_name})")
                    diff_text = self.vcs.get_diff(worktree_dir)
                    review_prompt = (
                        f"Perform an independent code review of this PR branch `{branch}`.\n"
                        f"Check for bugs, crash hazards, accessibility issues (<48dp), and security flaws.\n"
                        f"Mark any blocking issues with [P1] and non-blocking nits with [P2].\n"
                        f"Diff:\n```\n{diff_text[-3000:]}\n```"
                    )
                    rev_res = await reviewer.run(review_prompt, worktree_dir, effort="low", timeout_seconds=900)
                    review_findings = parse_review_findings(f"{rev_res.stdout}\n{rev_res.stderr}")
                    p1_findings = [f for f in review_findings if f.priority == "P1"]

                    # P1 gate: any open P1 blocks merge -> trigger one fix round
                    if p1_findings and round_num >= max_retries:
                        return CiWatchResult(
                            passed=False, rounds_used=round_num,
                            detail="Unresolved P1 review findings: " + "; ".join(f.message for f in p1_findings),
                            last_engine=active_engine, review_findings=review_findings,
                        )
                    if p1_findings and round_num < max_retries:
                        if on_status_change:
                            on_status_change("FIXING_REVIEW_P1")
                        findings_text = "\n".join(f"- {f.message}" for f in p1_findings)
                        fix_review_prompt = (
                            f"{guardrail_header}\n\nIndependent reviewer ({rev_engine_name}) found blocking [P1] issues:\n"
                            f"{findings_text}\n\nFix all P1 issues, commit, and push to `{branch}`."
                        )
                        prompt_path = worktree_dir / ".nightshift_prompt.md"
                        prompt_path.write_text(fix_review_prompt, encoding="utf-8")

                        runner_obj = self.runners.get(active_engine) or list(self.runners.values())[0]
                        await runner_obj.run(fix_review_prompt, worktree_dir, effort="medium", timeout_seconds=1800)
                        # Re-verify CI after review fix
                        continue

                pr_snap = self.vcs.find_open_pr(repo, branch)
                if not checked_sha or not pr_snap or str(pr_snap.get("headRefOid") or "") != checked_sha:
                    return CiWatchResult(passed=False, detail="PR head changed or could not be verified during CI and review.", last_engine=active_engine)
                return CiWatchResult(
                    passed=True,
                    flaky=flaky_detected,
                    rounds_used=round_num,
                    detail=f"CI green: {ci_msg}",
                    last_engine=active_engine,
                    review_findings=review_findings,
                    checked_sha=checked_sha,
                )

            # CI failed: check if retries exhausted
            if round_num >= max_retries:
                return CiWatchResult(
                    passed=False,
                    rounds_used=round_num,
                    detail=f"CI failed after {max_retries} fix attempts. Last error: {ci_msg}",
                    last_engine=active_engine,
                )

            # Pull failed CI log
            failed_log = self.vcs.get_failed_ci_log(repo, branch)

            # Same-error detector & engine rotation (BLUEPRINT 5d):
            # Rotate engine after 2 failed rounds by the same engine
            engine_failure_count += 1
            rotate, park = self.scheduler.same_error_detector.record_error(phase_id, active_engine, failed_log)
            if park:
                return CiWatchResult(
                    passed=False,
                    rounds_used=round_num + 1,
                    detail=f"Phase parked: identical failure signature across 3 engines. Log: {failed_log[:200]}",
                    last_engine=active_engine,
                )

            if engine_failure_count >= 2 or rotate:
                # Rotate engine to fresh eyes
                new_choice = self.scheduler.select_engine(
                    phase_id=phase_id,
                    step_kind="fix_ci",
                    current_engine=active_engine,
                    exclude_engines={active_engine},
                )
                if new_choice:
                    active_engine = new_choice.engine
                    engine_failure_count = 0
            else:
                # Fast switch-back at CI-fix round boundary (BLUEPRINT 5c)
                sb_choice = self.scheduler.select_engine(
                    phase_id=phase_id,
                    step_kind="fix_ci",
                    current_engine=active_engine,
                )
                if sb_choice:
                    if sb_choice.engine != active_engine:
                        engine_failure_count = 0
                    active_engine = sb_choice.engine
                else:
                    return CiWatchResult(passed=False, rounds_used=round_num, detail="No engine available for CI fix.", last_engine=active_engine)

            if not self.scheduler.has_run_budget_remaining():
                return CiWatchResult(
                    passed=False,
                    rounds_used=round_num,
                    detail="Runaway protection: max AI invocations per run reached during CI fix.",
                    last_engine=active_engine,
                )

            if on_status_change:
                on_status_change(f"AUTO_FIX_{round_num + 1} ({active_engine})")

            # Check for merge conflicts with main first (rebase-free update per BLUEPRINT 5d)
            merge_ok, merge_msg = self.vcs.merge_main_into_branch(worktree_dir)
            conflict_note = ""
            if not merge_ok:
                conflict_note = f"\n\nMERGE CONFLICT with origin/main: {merge_msg}\nResolve conflicts before pushing."

            fix_prompt = (
                f"{guardrail_header}\n\nCI failed on PR #{pr_number} (branch `{branch}`).\n"
                f"Fix the root cause, commit, and push to `{branch}`.{conflict_note}\n\n"
                f"Failed CI Log Tail:\n```\n{failed_log[-2000:]}\n```"
            )
            prompt_file = worktree_dir / ".nightshift_prompt.md"
            prompt_file.write_text(fix_prompt, encoding="utf-8")

            runner = self.runners.get(active_engine) or self.runners.get(active_engine.split("-")[0])
            if not runner:
                return CiWatchResult(passed=False, rounds_used=round_num, detail=f"Selected CI fixer {active_engine} is unavailable.", last_engine=active_engine)

            sha_before = self.vcs.remote_branch_sha(local_repo, branch)
            self.scheduler.record_invocation()
            fix_timeout = self.scheduler.invocation_timeout_seconds(phase_id, active_engine, 2400)
            await runner.run(
                f"Read and fix the CI failure in .nightshift_prompt.md, commit, and push to {branch}.",
                worktree_dir,
                effort="medium",
                timeout_seconds=fix_timeout,
            )

            # Verify that a new commit was pushed (NO-OP check per BLUEPRINT 5d)
            sha_after = self.vcs.remote_branch_sha(local_repo, branch)
            if not sha_after or sha_after == sha_before:
                # Try checkpointing uncommitted work if runner forgot to push
                self.vcs.create_checkpoint_commit_and_push(worktree_dir, branch, f"nightshift: auto-fix round {round_num + 1}")

        return CiWatchResult(
            passed=False,
            rounds_used=max_retries,
            detail="Exceeded maximum CI fix rounds.",
            last_engine=active_engine,
        )

    def handle_post_merge_failure(
        self,
        local_repo: Path,
        merge_commit_sha: str,
        phase_id: str,
        failure_log: str,
        repo: str,
    ) -> tuple[bool, str]:
        """Automatic revert PR on main build failure per BLUEPRINT 5d."""
        ok, msg = self.vcs.revert_via_pr(repo, local_repo, merge_commit_sha)
        if ok:
            # Re-queue phase with failure log
            self.store.park_phase(
                phase_id=phase_id,
                reason=f"Post-merge main build failed: {failure_log[:200]}",
                next_step="Investigate main build failure and rebase phase branch onto reverted main.",
            )
            return True, f"Reverted {merge_commit_sha[:7]} on main and re-queued phase {phase_id}."
        return False, f"Failed to revert {merge_commit_sha[:7]}: {msg}"
