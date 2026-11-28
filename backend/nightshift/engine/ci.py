"""GitHub Actions CI watcher, flaky-test retry, engine rotation, and self-healing fix loop (BLUEPRINT 5d)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nightshift.engine.pipeline_contracts import canonical_contract_json, provider_for_engine
from nightshift.engine.pipeline_review import (
    ValidatedReviewResult,
    build_isolated_review_prompt,
    classify_task_risk,
    parse_and_validate_review_output,
    select_independent_reviewer,
    tier_for_engine,
)
from nightshift.engine.scheduler import Scheduler
from nightshift.engine.vcs import GitGhClient
from nightshift.engine.verify import RiskAssessment, check_diff_guards
from nightshift.runners.base import Runner, RunnerResult
from nightshift.store import Store


@dataclass(frozen=True)
class ReviewFinding:
    priority: str  # "P1" or "P2"
    message: str
    file_path: str | None = None
    line: int | None = None
    kind: str = "defect"
    acceptance_criteria_id: str | None = None


@dataclass(frozen=True)
class CiWatchResult:
    passed: bool
    flaky: bool = False
    rounds_used: int = 0
    detail: str = ""
    last_engine: str = ""
    review_findings: list[ReviewFinding] = field(default_factory=list)
    checked_sha: str | None = None
    expected_base_sha: str | None = None
    ci_jobs: list[Any] = field(default_factory=list)
    reviewer_engine: str = ""
    reviewer_provider: str = ""
    reviewer_tier: str = ""
    contributing_engines: list[str] = field(default_factory=list)
    effective_risk_class: str = ""
    block_reason: str = ""
    review_result: ValidatedReviewResult | None = None


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

    def select_reviewer(
        self,
        implementer_engine: str,
        *,
        contributing_providers: set[str] | None = None,
        risk_class: str = "business_logic",
        graduated_economy_engines: set[str] | None = None,
    ) -> tuple[str, Runner] | tuple[None, None]:
        """Select an independent cross-provider reviewer (AGENT_PIPELINE §§2, 6.1, 9.2).

        Excludes ALL ``contributing_providers`` (coders, fixers, handoffs; ``agy`` and
        ``gemini`` both map to ``google``). NEVER falls back to self-review.
        """
        excluded = {provider_for_engine(p) for p in (contributing_providers or set()) if p}
        if implementer_engine:
            imp_prov = provider_for_engine(implementer_engine)
            if imp_prov:
                excluded.add(imp_prov)

        engine_tiers = {
            str(e.get("id") or ""): str(e.get("cost_tier") or "")
            for e in self.store.list_engines()
            if e.get("id")
        }
        chosen, runner, _reason = select_independent_reviewer(
            contributing_providers=excluded,
            risk_class=risk_class,
            runners=self.runners,
            is_available=self.scheduler.is_engine_available,
            engine_tiers=engine_tiers,
            graduated_economy_engines=graduated_economy_engines,
        )
        if chosen and runner:
            return chosen, runner
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
        task_contract: dict[str, Any] | None = None,
        contributing_providers: set[str] | None = None,
        risk_class: str | None = None,
        critical_paths: list[str] | None = None,
        max_review_fix_rounds: int = 2,
        require_independent_reviewer: bool = True,
    ) -> CiWatchResult:
        """Watch CI checks and auto-fix failures up to max_retries with engine rotation."""
        active_engine = initial_engine
        engine_failure_count = 0
        flaky_detected = False
        review_fix_rounds_used = 0
        contributing_engines: list[str] = []
        all_contributing_providers: set[str] = {
            provider_for_engine(p) for p in (contributing_providers or set()) if p
        }
        if initial_engine and initial_engine not in ("unknown", "none"):
            init_prov = provider_for_engine(initial_engine)
            if init_prov:
                all_contributing_providers.add(init_prov)

        for round_num in range(max_retries + 1):
            if on_status_change:
                on_status_change(f"WATCHING_CI ({round_num + 1}/{max_retries + 1})")

            # Bind the check and review to a stable PR head and base branch SHA.
            expected_base_sha = self.vcs.remote_branch_sha(local_repo, "main")
            snapshot = self.vcs.find_open_pr(repo, branch)
            checked_sha = str(snapshot.get("headRefOid") or "") if snapshot else ""
            if not checked_sha or self.vcs.local_head_sha(worktree_dir) != checked_sha:
                return CiWatchResult(passed=False, detail="Local checkout does not match the PR head.", last_engine=active_engine)
            passed, ci_msg = self.vcs.watch_pr_checks(repo, pr_number, worktree_dir)

            # 1. Flaky test detector: if failed, re-run checks once before burning a fix round
            if not passed and round_num == 0:
                rerun_passed, rerun_msg = self.vcs.watch_pr_checks(repo, pr_number, worktree_dir)
                if rerun_passed:
                    flaky_detected = True
                    passed = True
                    ci_msg = "Passed on re-run without code change (flaky test)"

            if passed:
                # CI is green! Now run Risk-based independent review (AGENT_PIPELINE §§6.1, 9.2)
                review_findings: list[ReviewFinding] = []
                verified_reviewer_engine = ""
                verified_reviewer_provider = ""
                verified_reviewer_tier = ""
                validated_review: ValidatedReviewResult | None = None
                effective_risk_class = str(risk_class or (task_contract or {}).get("risk_class") or "business_logic")

                diff_text = self.vcs.get_diff(worktree_dir)
                changed_files = self.vcs.list_changed_files(worktree_dir)
                diff_entries = self.vcs.list_changed_entries(worktree_dir)
                test_snapshots = self.vcs.get_test_file_snapshots(worktree_dir, diff_entries)
                contract_allowed_globs = (
                    json.loads(str(task_contract.get("allowed_globs_json") or "[]"))
                    if task_contract is not None
                    else None
                )
                contract_forbidden_globs = (
                    json.loads(str(task_contract.get("forbidden_globs_json") or "[]"))
                    if task_contract is not None
                    else None
                )
                guard_findings = check_diff_guards(
                    worktree_dir,
                    changed_files,
                    diff_text,
                    diff_entries=diff_entries,
                    allowed_globs=contract_allowed_globs,
                    forbidden_globs=contract_forbidden_globs,
                    test_file_snapshots=test_snapshots,
                )
                if guard_findings:
                    for finding in guard_findings:
                        self.store.record_guard_result(phase_id, finding.guard, passed=False, detail=finding.message)
                    return CiWatchResult(
                        passed=False,
                        rounds_used=round_num,
                        detail="Diff guard failed: " + "; ".join(f.message for f in guard_findings),
                        last_engine=active_engine,
                        checked_sha=checked_sha,
                        expected_base_sha=expected_base_sha,
                        contributing_engines=list(contributing_engines),
                        effective_risk_class=effective_risk_class,
                        block_reason="diff_guard_failed",
                    )

                if task_contract is not None:
                    raw_factors = task_contract.get("risk_factors_json")
                    try:
                        risk_factors = json.loads(str(raw_factors)) if raw_factors else None
                    except Exception:
                        risk_factors = None
                    classification = classify_task_risk(
                        diff_entries,
                        diff_text,
                        base_risk_class=effective_risk_class,
                        risk_factors=risk_factors,
                        critical_paths=critical_paths,
                    )
                    effective_risk_class = classification.risk_class

                    if require_independent_reviewer and classification.requires_independent_review:
                        rev_engine_name, reviewer = self.select_reviewer(
                            active_engine,
                            contributing_providers=all_contributing_providers,
                            risk_class=effective_risk_class,
                        )
                        if not reviewer or not rev_engine_name:
                            return CiWatchResult(
                                passed=False,
                                rounds_used=round_num,
                                detail=(
                                    f"No independent reviewer available for risk class '{effective_risk_class}' "
                                    f"outside contributing providers {sorted(all_contributing_providers)}."
                                ),
                                last_engine=active_engine,
                                checked_sha=checked_sha,
                                expected_base_sha=expected_base_sha,
                                contributing_engines=list(contributing_engines),
                                effective_risk_class=effective_risk_class,
                                block_reason="no_independent_reviewer",
                            )

                        if on_status_change:
                            on_status_change(f"REVIEWING ({rev_engine_name})")

                        rev_provider = provider_for_engine(rev_engine_name)
                        engine_tiers_lookup = {
                            str(e.get("id") or ""): str(e.get("cost_tier") or "")
                            for e in self.store.list_engines()
                            if e.get("id")
                        }
                        rev_tier = tier_for_engine(
                            rev_engine_name,
                            explicit_tier=engine_tiers_lookup.get(rev_engine_name) or None,
                        )
                        contract_json = str(
                            task_contract.get("canonical_json")
                            or canonical_contract_json(task_contract)
                        )

                        # Strict input isolation: raw diff + frozen contract ONLY (no PR narrative)
                        review_prompt = build_isolated_review_prompt(
                            contract_canonical_json=contract_json,
                            raw_diff=diff_text,
                            head_sha=checked_sha,
                        )
                        rev_res = await reviewer.run(review_prompt, worktree_dir, effort="low", timeout_seconds=900)
                        validated = parse_and_validate_review_output(
                            rev_res.stdout or "",
                            worktree_dir=worktree_dir,
                            contract=task_contract,
                            reviewer_engine=rev_engine_name,
                            reviewer_provider=rev_provider,
                            reviewer_tier=rev_tier,
                        )
                        # Retry ONCE with schema/citation errors if invalid (AGENT_PIPELINE §9.2)
                        if (rev_res.rc != 0 or rev_res.timed_out or not validated.valid) and not rev_res.timed_out:
                            retry_errors = validated.errors or [f"Reviewer exited with rc={rev_res.rc}"]
                            retry_prompt = build_isolated_review_prompt(
                                contract_canonical_json=contract_json,
                                raw_diff=diff_text,
                                head_sha=checked_sha,
                                retry_validation_errors=retry_errors,
                            )
                            rev_res = await reviewer.run(retry_prompt, worktree_dir, effort="low", timeout_seconds=900)
                            validated = parse_and_validate_review_output(
                                rev_res.stdout or "",
                                worktree_dir=worktree_dir,
                                contract=task_contract,
                                reviewer_engine=rev_engine_name,
                                reviewer_provider=rev_provider,
                                reviewer_tier=rev_tier,
                            )

                        if rev_res.rc != 0 or rev_res.timed_out or not validated.valid:
                            err_detail = "; ".join(validated.errors) if validated.errors else f"rc={rev_res.rc}"
                            return CiWatchResult(
                                passed=False,
                                rounds_used=round_num,
                                detail=f"Independent review output invalid: {err_detail}",
                                last_engine=active_engine,
                                checked_sha=checked_sha,
                                expected_base_sha=expected_base_sha,
                                reviewer_engine=rev_engine_name,
                                reviewer_provider=rev_provider,
                                reviewer_tier=rev_tier,
                                contributing_engines=list(contributing_engines),
                                effective_risk_class=effective_risk_class,
                                block_reason="invalid_review_output",
                                review_result=validated,
                            )

                        validated_review = validated
                        review_findings = [
                            ReviewFinding(
                                priority=f.priority,
                                message=f.message,
                                file_path=f.file,
                                line=f.line,
                                kind=f.kind,
                                acceptance_criteria_id=f.acceptance_criteria_id,
                            )
                            for f in validated.findings
                        ]
                        p1_findings = [f for f in review_findings if f.priority == "P1"]

                        if p1_findings or validated.verdict == "CHANGES":
                            if review_fix_rounds_used >= max_review_fix_rounds or round_num >= max_retries:
                                return CiWatchResult(
                                    passed=False,
                                    rounds_used=round_num,
                                    detail="Unresolved P1 review findings: " + "; ".join(f.message for f in p1_findings),
                                    last_engine=active_engine,
                                    review_findings=review_findings,
                                    checked_sha=checked_sha,
                                    expected_base_sha=expected_base_sha,
                                    reviewer_engine=rev_engine_name,
                                    reviewer_provider=rev_provider,
                                    reviewer_tier=rev_tier,
                                    contributing_engines=list(contributing_engines),
                                    effective_risk_class=effective_risk_class,
                                    block_reason="unresolved_p1_findings",
                                    review_result=validated,
                                )

                            review_fix_rounds_used += 1
                            if on_status_change:
                                on_status_change("FIXING_REVIEW_P1")
                            findings_text = "\n".join(
                                f"- [{f.priority}] {f.file_path or ''}:{f.line or ''} {f.message}"
                                for f in (p1_findings or review_findings)
                            )
                            fix_review_prompt = (
                                f"{guardrail_header}\n\nIndependent reviewer ({rev_engine_name}) found blocking [P1] issues:\n"
                                f"{findings_text}\n\nFix all P1 issues, commit, and push to `{branch}`."
                            )
                            prompt_path = worktree_dir / ".nightshift_prompt.md"
                            prompt_path.write_text(fix_review_prompt, encoding="utf-8")

                            if not self.scheduler.has_run_budget_remaining():
                                return CiWatchResult(
                                    passed=False,
                                    rounds_used=round_num,
                                    detail="Runaway protection: max AI invocations per run reached during review fix.",
                                    last_engine=active_engine,
                                    review_findings=review_findings,
                                    contributing_engines=list(contributing_engines),
                                    effective_risk_class=effective_risk_class,
                                    block_reason="budget_exhausted",
                                )
                            choice = self.scheduler.select_engine(phase_id=phase_id, step_kind="fix_ci", current_engine=active_engine)
                            if not choice:
                                return CiWatchResult(
                                    passed=False,
                                    rounds_used=round_num,
                                    detail="No engine available for review fix.",
                                    last_engine=active_engine,
                                    review_findings=review_findings,
                                    contributing_engines=list(contributing_engines),
                                    effective_risk_class=effective_risk_class,
                                    block_reason="no_fixer_available",
                                )
                            active_engine = choice.engine
                            runner_obj = self.runners.get(active_engine) or self.runners.get(active_engine.split("-")[0])
                            if not runner_obj:
                                return CiWatchResult(
                                    passed=False,
                                    rounds_used=round_num,
                                    detail=f"Selected review fixer {active_engine} is unavailable.",
                                    last_engine=active_engine,
                                    review_findings=review_findings,
                                    contributing_engines=list(contributing_engines),
                                    effective_risk_class=effective_risk_class,
                                    block_reason="no_fixer_available",
                                )
                            self.scheduler.record_invocation()
                            self.scheduler.quota_ledger.record_usage(active_engine, choice.model)
                            timeout = self.scheduler.invocation_timeout_seconds(phase_id, active_engine, 1800)
                            if active_engine not in contributing_engines:
                                contributing_engines.append(active_engine)
                            fix_prov = provider_for_engine(active_engine)
                            if fix_prov:
                                all_contributing_providers.add(fix_prov)
                            await runner_obj.run(fix_review_prompt, worktree_dir, model=choice.model or None, effort=choice.effort, timeout_seconds=timeout)
                            continue

                        verified_reviewer_engine = rev_engine_name
                        verified_reviewer_provider = rev_provider
                        verified_reviewer_tier = rev_tier
                else:
                    # Legacy non-contract review mode (still enforces provider independence, no self-fallback)
                    rev_engine_name, reviewer = self.select_reviewer(
                        active_engine,
                        contributing_providers=all_contributing_providers,
                    )
                    if reviewer and rev_engine_name:
                        if on_status_change:
                            on_status_change(f"REVIEWING ({rev_engine_name})")
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
                                contributing_engines=list(contributing_engines),
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

                            if not self.scheduler.has_run_budget_remaining():
                                return CiWatchResult(passed=False, rounds_used=round_num, detail="Runaway protection: max AI invocations per run reached during review fix.", last_engine=active_engine, review_findings=review_findings, contributing_engines=list(contributing_engines))
                            choice = self.scheduler.select_engine(phase_id=phase_id, step_kind="fix_ci", current_engine=active_engine)
                            if not choice:
                                return CiWatchResult(passed=False, rounds_used=round_num, detail="No engine available for review fix.", last_engine=active_engine, review_findings=review_findings, contributing_engines=list(contributing_engines))
                            active_engine = choice.engine
                            runner_obj = self.runners.get(active_engine) or self.runners.get(active_engine.split("-")[0])
                            if not runner_obj:
                                return CiWatchResult(passed=False, rounds_used=round_num, detail=f"Selected review fixer {active_engine} is unavailable.", last_engine=active_engine, review_findings=review_findings, contributing_engines=list(contributing_engines))
                            self.scheduler.record_invocation()
                            self.scheduler.quota_ledger.record_usage(active_engine, choice.model)
                            timeout = self.scheduler.invocation_timeout_seconds(phase_id, active_engine, 1800)
                            if active_engine not in contributing_engines:
                                contributing_engines.append(active_engine)
                            fix_prov = provider_for_engine(active_engine)
                            if fix_prov:
                                all_contributing_providers.add(fix_prov)
                            await runner_obj.run(fix_review_prompt, worktree_dir, model=choice.model or None, effort=choice.effort, timeout_seconds=timeout)
                            # Re-verify CI after review fix
                            continue

                        if rev_res.rc == 0 and not rev_res.timed_out and not p1_findings:
                            verified_reviewer_engine = rev_engine_name
                            verified_reviewer_provider = provider_for_engine(rev_engine_name)
                            verified_reviewer_tier = tier_for_engine(rev_engine_name)

                pr_snap = self.vcs.find_open_pr(repo, branch)
                if not checked_sha or not pr_snap or str(pr_snap.get("headRefOid") or "") != checked_sha:
                    return CiWatchResult(passed=False, detail="PR head changed or could not be verified during CI and review.", last_engine=active_engine, contributing_engines=list(contributing_engines))
                ci_jobs = self.vcs.get_commit_ci_checks(repo, checked_sha, branch=branch)
                return CiWatchResult(
                    passed=True,
                    flaky=flaky_detected,
                    rounds_used=round_num,
                    detail=f"CI green: {ci_msg}",
                    last_engine=active_engine,
                    review_findings=review_findings,
                    checked_sha=checked_sha,
                    expected_base_sha=expected_base_sha,
                    ci_jobs=ci_jobs,
                    reviewer_engine=verified_reviewer_engine,
                    reviewer_provider=verified_reviewer_provider,
                    reviewer_tier=verified_reviewer_tier,
                    contributing_engines=list(contributing_engines),
                    effective_risk_class=effective_risk_class,
                    review_result=validated_review,
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
            if active_engine not in contributing_engines:
                contributing_engines.append(active_engine)
            fix_prov = provider_for_engine(active_engine)
            if fix_prov:
                all_contributing_providers.add(fix_prov)
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
        """Open a revert PR on main build failure without auto-merging (must pass review & MergeGate)."""
        ok, msg = self.vcs.open_revert_pr(repo, local_repo, merge_commit_sha)
        if ok:
            # Park phase with failure log; revert PR stays open for review & MergeGate
            self.store.park_phase(
                phase_id=phase_id,
                reason=f"Post-merge main build failed: {failure_log[:200]}",
                next_step="Review and merge the opened revert PR via MergeGate, then rebase phase branch.",
            )
            return True, f"Reverted {merge_commit_sha[:7]} via open revert PR (awaiting review and MergeGate) and parked phase {phase_id}."
        return False, f"Failed to open revert PR for {merge_commit_sha[:7]}: {msg}"
