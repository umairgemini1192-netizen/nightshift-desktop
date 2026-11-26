"""Durable phase state machine with checkpointing, handoff, and recovery (BLUEPRINT 5, 5c, 5d)."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from nightshift.engine.ci import CiPipelineManager, CiWatchResult
from nightshift.engine.deck import generate_deck_data, write_morning_digest
from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.planner import PhaseDefinition, get_runnable_phases, resolve_base_branch, validate_plan_dag
from nightshift.engine.release import ReleaseManager
from nightshift.engine.scheduler import Scheduler
from nightshift.engine.vcs import GitGhClient, SubprocessGitGh
from nightshift.engine.verify import assess_diff_risk, check_diff_guards, redact_secrets, verify_deliverable
from nightshift.events import Event, EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.base import Runner, RunnerResult
from nightshift.runners.template import TemplateRunner
from nightshift.store import Store

logger = logging.getLogger(__name__)

WORKTREE_ROOT = Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp"))) / "nightshift-worktrees"


def set_keep_awake(enable: bool) -> bool:
    """Keep the PC awake via SetThreadExecutionState on Windows (BLUEPRINT 5d)."""
    if sys.platform != "win32":
        return False
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if enable else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return True
    except Exception as e:
        logger.warning("Could not set Windows keep-awake state: %e", e)
        return False


@dataclass
class ProjectConfig:
    id: str
    name: str
    repo: str
    local_path: Path
    build_rules: str = ""
    ui_contract: str = ""
    release_workflow: str = "Android build"
    artifact_glob: str = "*.apk"
    artifact_prefix: str = ""
    tag_prefix: str = "v1.0.0-preview."
    output_dir: Path = field(default_factory=lambda: Path("dist"))
    critical_paths: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectConfig:
        raw_cp = data.get("critical_paths") or data.get("critical_paths_json") or []
        if isinstance(raw_cp, str):
            try:
                cp = json.loads(raw_cp)
            except Exception:
                cp = []
        else:
            cp = list(raw_cp)

        out = data.get("output_dir") or data.get("output_folder") or "dist"
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", data["id"])),
            repo=str(data["repo"]),
            local_path=Path(data["local_path"]),
            build_rules=str(data.get("build_rules", "")),
            ui_contract=str(data.get("ui_contract", "")),
            release_workflow=str(data.get("release_workflow", "Android build")),
            artifact_glob=str(data.get("artifact_glob", "*.apk")),
            artifact_prefix=str(data.get("artifact_prefix", "")),
            tag_prefix=str(data.get("tag_prefix", "v1.0.0-preview.")),
            output_dir=Path(out),
            critical_paths=cp,
        )


class SingleInstanceLock:
    """File lock preventing multiple engine instances on the same project (BLUEPRINT 5d)."""

    def __init__(self, project_id: str, lock_dir: Path | None = None) -> None:
        self.project_id = project_id
        base_dir = lock_dir or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NightShift" / "locks")
        base_dir.mkdir(parents=True, exist_ok=True)
        # Project IDs can contain repo separators or other filesystem metacharacters.
        lock_key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        self.lock_file = base_dir / f"{lock_key}.lock"
        self._acquired = False

    def acquire(self) -> bool:
        if self.lock_file.exists():
            try:
                old_pid = int(self.lock_file.read_text(encoding="utf-8").strip())
                # Check if process is still running on Windows
                if sys.platform == "win32":
                    import subprocess
                    check = subprocess.run(["tasklist", "/FI", f"PID eq {old_pid}"], capture_output=True, text=True)
                    if str(old_pid) in check.stdout:
                        return False
            except Exception:
                pass
        try:
            self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
            self._acquired = True
            return True
        except Exception:
            return False

    def release(self) -> None:
        if self._acquired and self.lock_file.exists():
            try:
                self.lock_file.unlink()
            except Exception:
                pass
            self._acquired = False


class EngineStateMachine:
    """Main state machine orchestrating end-to-end overnight runs with full persistence and self-healing."""

    def __init__(
        self,
        store: Store,
        event_bus: EventBus,
        quota_ledger: QuotaLedger,
        vcs: GitGhClient | None = None,
        runners: dict[str, Runner] | None = None,
        login_checker: LoginHealthChecker | None = None,
        network_backoff_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.events = event_bus
        self.quota = quota_ledger
        self.vcs = vcs or SubprocessGitGh()
        self.runners = runners or {}
        self.logins = login_checker or LoginHealthChecker(store=store, quota_ledger=quota_ledger)
        self._network_backoff_seconds = network_backoff_seconds

        self.scheduler = Scheduler(
            store=store,
            quota_ledger=quota_ledger,
            login_checker=self.logins,
        )
        self.ci_pipeline = CiPipelineManager(
            vcs=self.vcs,
            store=store,
            scheduler=self.scheduler,
            runners=self.runners,
        )
        self.release_mgr = ReleaseManager(vcs=self.vcs)

        self._stop_requested = False
        self._is_running = False

    def request_stop(self) -> None:
        """Signal the engine to halt cleanly at the current phase boundary (BLUEPRINT 5, 5d)."""
        self._stop_requested = True
        logger.info("Stop requested at phase boundary.")

    def cancel_stop(self) -> None:
        """Cancel an armed stop-after-current-phase request (BLUEPRINT 5e)."""
        self._stop_requested = False
        logger.info("Stop request cancelled.")

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def is_running(self) -> bool:
        return self._is_running

    def pre_flight_checks(self, project: ProjectConfig) -> tuple[bool, str]:
        """Verify disk space, git/gh auth, network reachability, and repo state (BLUEPRINT 5d)."""
        # 1. Disk space (> 5 GB free)
        try:
            total, used, free = shutil.disk_usage(project.local_path if project.local_path.exists() else Path("."))
            if free < 5 * 1024 * 1024 * 1024:
                return False, f"Low disk space: {free / (1024*1024*1024):.1f} GB free (< 5 GB required)"
        except Exception:
            pass

        # 2. Network and VCS health
        git_stat = self.logins.check_tool("git", repo=project.repo, force=True)
        if git_stat.state in ("Logged out", "Unknown (network)"):
            return False, f"Git pre-flight failed: {git_stat.detail}"

        gh_stat = self.logins.check_tool("gh", repo=project.repo, force=True)
        if gh_stat.state == "Logged out":
            return False, f"GitHub CLI not logged in: {gh_stat.detail}"

        # 3. Branch protection check
        protected = self.vcs.check_branch_protection(project.repo, branch="main")
        if not protected:
            logger.warning("Branch protection is not enabled on main for %s", project.repo)
            self.events.emit(
                Event(
                    kind="branch_protection.missing",
                    plain_text=f"Warning: GitHub branch protection is not enabled on main for {project.repo}.",
                    level="warn",
                )
            )

        return True, "Pre-flight checks passed"

    async def execute_plan(
        self,
        project: ProjectConfig,
        phases: list[PhaseDefinition],
        *,
        plan_id: str = "plan_default",
    ) -> list[dict[str, Any]]:
        """Run the continuous failover loop across all plan phases."""
        self._stop_requested = False
        self._is_running = True
        set_keep_awake(True)

        lock = SingleInstanceLock(project.id)
        if not lock.acquire():
            set_keep_awake(False)
            self._is_running = False
            raise RuntimeError(f"Another NightShift process is already running for project {project.id}")

        summary_rows: list[dict[str, Any]] = []
        stopped_emitted = False

        try:
            # Validate plan DAG
            ordered_phases = validate_plan_dag(phases)
            phases_by_id = {p.id: p for p in ordered_phases}

            # Ensure project exists in store
            if self.store.get_project(project.id) is None:
                self.store.create_project(
                    project.id,
                    project.name,
                    project.repo,
                    str(project.local_path),
                    build_rules=project.build_rules,
                    ui_contract=project.ui_contract,
                    release_workflow=project.release_workflow,
                    artifact_glob=project.artifact_glob,
                    artifact_prefix=project.artifact_prefix,
                    tag_prefix=project.tag_prefix,
                    output_dir=str(project.output_dir),
                    critical_paths_json=json.dumps(project.critical_paths),
                )

            self.scheduler.max_implement_attempts = int(self.store.get_setting("guard.maxAttempts", str(self.scheduler.max_implement_attempts)))
            self.scheduler.max_ci_fix_rounds = int(self.store.get_setting("guard.maxCiRetries", str(self.scheduler.max_ci_fix_rounds)))
            self.scheduler.max_run_invocations = int(self.store.get_setting("guard.maxRunInvocations", str(self.scheduler.max_run_invocations)))
            self.scheduler.total_invocations_count = 0

            # Persist plan in store
            if not self.store.get_plan(plan_id):
                self.store.create_plan(plan_id, project.id, f"Run for {project.name}")
            for p in ordered_phases:
                previous = self.store.get_phase(p.id) or {}
                self.store.upsert_phase(
                    p.id,
                    plan_id,
                    p.title,
                    p.branch,
                    prompt=p.prompt,
                    depends_on_json=json.dumps(p.depends_on),
                    size=p.size, ladder=p.ladder, timeout_minutes=p.timeout_minutes,
                    auto_merge=p.auto_merge, release_after_merge=p.release_after_merge, force_rerun=p.force_rerun,
                    position=p.position,
                    status=previous.get("status", "pending"),
                    attempt=previous.get("attempt", 0),
                    pr_number=previous.get("pr_number"),
                )

            self.events.emit(Event(
                kind="run.started",
                plain_text=f"Started run for {project.name}.",
                level="info",
                payload={"project_id": project.id},
            ))

            completed_ids: set[str] = set()
            parked_ids: set[str] = set()

            for phase in ordered_phases:
                # 1. Honour Stop-after-current-phase at boundary
                if self._stop_requested:
                    stopped_emitted = True
                    self.events.emit(Event(
                        kind="run.stopped",
                        plain_text="Stop after current phase requested. Halting cleanly at phase boundary.",
                        level="info",
                        phase_id=phase.id,
                        payload={"project_id": project.id},
                    ))
                    break

                # 2. Check circuit breaker (3 parked phases in a row -> stop starting new phases)
                if self.scheduler.circuit_breaker.is_tripped:
                    self.events.emit(Event(
                        kind="circuit_breaker.tripped",
                        plain_text="Circuit breaker tripped after 3 consecutive parked phases. Pausing new phase starts.",
                        level="warn",
                        phase_id=phase.id,
                        payload={"project_id": project.id},
                    ))
                    break

                # 3. Skip already merged phases unless force_rerun
                if not phase.force_rerun:
                    merged_pr = self.vcs.find_merged_pr(project.repo, phase.branch)
                    merge_sha = ((merged_pr or {}).get("mergeCommit") or {}).get("oid")
                    if merge_sha and self.vcs.find_merged_pr(project.repo, f"nightshift/revert-{merge_sha}"):
                        merged_pr = None
                        parked = self.store.get_parked(phase.id) or {}
                        phase = replace(phase, prompt=(
                            f"{phase.prompt}\n\nThe previous merge {merge_sha} was reverted after the main build failed. "
                            f"Restore the reverted changes on top of current main, fix the build failure, and open a new PR. "
                            f"Failure: {parked.get('reason', 'Inspect the failed main build.')}"
                        ))
                    if merged_pr:
                        prior = self.store.get_phase(phase.id) or {}
                        if prior.get("status") in ("merging", "merged", "releasing", "verifying_build") or (prior.get("status") == "parked" and prior.get("pr_number")):
                            row = self._finish_merged_delivery(project, phase, merged_pr, "none")
                            summary_rows.append(row)
                            if row["status"] == "COMPLETED":
                                completed_ids.add(phase.id)
                            else:
                                parked_ids.add(phase.id)
                            continue
                        self.store.update_phase(phase.id, status="merged", pr_number=merged_pr["number"])
                        completed_ids.add(phase.id)
                        summary_rows.append({
                            "phase_id": phase.id,
                            "title": phase.title,
                            "runner": "none",
                            "status": "SKIPPED",
                            "detail": f"Already merged PR #{merged_pr['number']}",
                        })
                        self.events.emit(Event(
                            kind="phase.skipped",
                            plain_text=f"Skipping {phase.title}: already merged via PR #{merged_pr['number']}.",
                            level="info",
                            phase_id=phase.id,
                        ))
                        continue

                # 4. Pre-flight check before phase
                pf_ok, pf_msg = self.pre_flight_checks(project)
                if not pf_ok:
                    # Network or git/gh auth loss backoff: pause without burning attempts (BLUEPRINT 5d, 5f)
                    if "network" in pf_msg.lower() or "git" in pf_msg.lower():
                        logger.warning("Transient pre-flight issue, pausing %.1fs without burning attempts: %s", self._network_backoff_seconds, pf_msg)
                        self.events.emit(Event(
                            kind="preflight.backoff",
                            plain_text=f"Pausing with backoff without burning attempts: {pf_msg}",
                            level="warn",
                            phase_id=phase.id,
                        ))
                        await asyncio.sleep(self._network_backoff_seconds)
                        pf_ok, pf_msg = self.pre_flight_checks(project)
                    if not pf_ok:
                        self._park_phase(project.id, phase, reason=f"Pre-flight failed: {pf_msg}", next_step="Fix pre-flight issue")
                        parked_ids.add(phase.id)
                        self.scheduler.circuit_breaker.record_phase_result(parked=True)
                        summary_rows.append({"phase_id": phase.id, "title": phase.title, "runner": "none", "status": "PARKED", "detail": pf_msg})
                        continue

                # 5. Determine base branch (stack on parent branch if parent is parked)
                base_branch = resolve_base_branch(phase, phases_by_id, parked_ids, default_base="main")
                if base_branch != "main":
                    for dep in phase.depends_on:
                        if dep in parked_ids:
                            parked_rec = self.store.get_parked(dep)
                            if parked_rec:
                                children = json.loads(parked_rec.get("stacked_children_json") or "[]")
                                if phase.id not in children:
                                    children.append(phase.id)
                                    self.store.park_phase(
                                        dep,
                                        reason=parked_rec["reason"],
                                        next_step=parked_rec["next_step"],
                                        stacked_children=children,
                                    )

                # 6. Execute single phase through state machine
                row = await self._execute_phase(project, phase, plan_id, base_branch, dependencies_merged=all(dep in completed_ids for dep in phase.depends_on))
                summary_rows.append(row)

                if row["status"] in ("COMPLETED", "MERGED"):
                    if self.vcs.find_merged_pr(project.repo, phase.branch):
                        completed_ids.add(phase.id)
                    else:
                        parked_ids.add(phase.id)
                    self.scheduler.circuit_breaker.record_phase_result(parked=False)
                elif row["status"] == "PARKED":
                    parked_ids.add(phase.id)
                    self.scheduler.circuit_breaker.record_phase_result(parked=True)

                # Update deck data & morning digest after each phase
                deck = generate_deck_data(self.store, project.id, current_phase_title=phase.title)
                write_morning_digest(project.output_dir, project.name, summary_rows, deck)

        finally:
            set_keep_awake(False)
            lock.release()
            self._is_running = False
            if not stopped_emitted:
                self.events.emit(Event(
                    kind="run.finished",
                    plain_text=f"Finished run for {project.name}.",
                    level="info",
                    payload={"project_id": project.id},
                ))

        return summary_rows

    def _park_phase(self, project_id: str, phase: PhaseDefinition, *, reason: str, next_step: str) -> None:
        self.store.park_phase(phase.id, reason=reason, next_step=next_step)
        self.events.emit(Event(
            kind="phase.parked",
            plain_text=f"Parked {phase.title}: {reason}",
            level="warn",
            phase_id=phase.id,
            payload={"project_id": project_id},
        ))

    async def _execute_phase(
        self,
        project: ProjectConfig,
        phase: PhaseDefinition,
        plan_id: str,
        base_branch: str,
        dependencies_merged: bool = True,
    ) -> dict[str, Any]:
        """Execute a single phase from worktree preparation through delivery and release."""
        if phase.branch.strip().lower() in ("main", "master"):
            raise ValueError(f"Hard guard violation: phase '{phase.id}' cannot target protected branch '{phase.branch}'.")

        wt_dir = WORKTREE_ROOT / phase.id.replace("/", "_").replace("\\", "_")
        active_runner_name = "unknown"
        t0 = time.time()

        # Step 1: Prepare worktree
        self.store.update_phase(phase.id, status="preparing_worktree")
        self.events.emit(Event(
            kind="phase.started",
            plain_text=f"Preparing worktree for {phase.title} on branch {phase.branch} (base: {base_branch}).",
            phase_id=phase.id,
            payload={"project_id": project.id},
        ))

        try:
            self.vcs.prepare_worktree(project.local_path, wt_dir, phase.branch, base_branch=base_branch)
        except Exception as e:
            self._park_phase(project.id, phase, reason=f"Worktree creation failed: {e}", next_step="Inspect repo / disk")
            return {"phase_id": phase.id, "title": phase.title, "runner": "none", "status": "PARKED", "detail": str(e)}

        guardrail_header = (
            "CONTINUOUS ORCHESTRATOR GUARDRAILS:\n"
            f"- Isolated worktree on branch `{phase.branch}`.\n"
            f"- Zero glitches: ensure clean compilation, strict tests, and zero regressions.\n"
            f"- Commit cleanly, push `{phase.branch}`, and open a PR targeting `main` (if PR exists, push to it).\n"
            "- NEVER merge to `main` or cut releases yourself; the orchestrator merges after CI passes.\n\n"
        )
        if project.ui_contract:
            guardrail_header += f"{project.ui_contract}\n\n"

        # Step 2: Check if PR already exists (resume at CI watch)
        existing_pr = self.vcs.find_open_pr(project.repo, phase.branch)
        pr_number: int | None = None
        pr_url: str | None = None

        if existing_pr:
            pr_number = int(existing_pr["number"])
            pr_url = str(existing_pr["url"])
            self.store.update_phase(phase.id, status="watching_ci", pr_number=pr_number)
            self.events.emit(Event(
                kind="phase.resumed",
                plain_text=f"PR #{pr_number} already open for {phase.branch} -> skipping implementation, resuming at CI watch.",
                phase_id=phase.id,
            ))
        else:
            # Implementation ladder loop with mid-phase handoff & fast switch-back
            self.store.update_phase(phase.id, status="implementing")
            self.events.emit(Event(
                kind="phase.implementing",
                plain_text=f"Starting implementation of {phase.title}.",
                phase_id=phase.id,
            ))

            prompt_path = wt_dir / ".nightshift_prompt.md"
            prompt_path.write_text(guardrail_header + phase.prompt, encoding="utf-8")

            implement_attempts = 0
            diff_guard_retries = 0
            delivered = False
            pending_choice = None

            while implement_attempts < self.scheduler.max_implement_attempts:
                if not self.scheduler.has_run_budget_remaining():
                    self._park_phase(
                        project.id,
                        phase,
                        reason="Runaway protection: max AI invocations per run reached",
                        next_step="Review run logs and resume next night",
                    )
                    return {
                        "phase_id": phase.id,
                        "title": phase.title,
                        "runner": active_runner_name,
                        "status": "PARKED",
                        "detail": "Max AI invocations per run reached",
                    }

                implement_attempts += 1
                self.store.update_phase(phase.id, attempt=implement_attempts)

                choice = pending_choice or self.scheduler.select_engine(
                    phase_id=phase.id,
                    step_kind="implement",
                    current_engine=active_runner_name if active_runner_name != "unknown" else None,
                )
                pending_choice = None
                if not choice:
                    # All engines exhausted -> cooldown wait
                    earliest = self.quota.earliest_cooldown_expiry()
                    earliest_str = earliest.strftime("%H:%M") if earliest else "soon"
                    self.events.emit(Event(
                        kind="quota.exhausted",
                        plain_text=f"All engines cooling down. Waiting for earliest reset at {earliest_str}.",
                        phase_id=phase.id,
                    ))
                    self._park_phase(project.id, phase, reason=f"All engines cooling down until {earliest_str}", next_step="Resume after cooldown")
                    return {"phase_id": phase.id, "title": phase.title, "runner": "none", "status": "PARKED", "detail": f"All engines in cooldown until {earliest_str}"}

                active_runner_name = choice.engine
                runner_obj = self.runners.get(active_runner_name)
                if not runner_obj:
                    eng_row = next(
                        (e for e in self.store.list_engines() if e["id"] == active_runner_name),
                        None,
                    ) or next(
                        (e for e in self.store.list_engines() if e["runner"] == active_runner_name),
                        None,
                    )
                    if eng_row:
                        runner_type = eng_row.get("runner", "")
                        if runner_type and runner_type in self.runners:
                            runner_obj = self.runners[runner_type]
                            self.runners[active_runner_name] = runner_obj
                        else:
                            env = self.scheduler.router.build_isolated_environment(eng_row["id"])
                            try:
                                patterns = json.loads(eng_row.get("quota_patterns_json") or "[]")
                            except Exception:
                                patterns = []
                            runner_obj = TemplateRunner(
                                eng_row["runner"],
                                eng_row.get("command_template") or [eng_row["runner"], "-p", "{instruction}"],
                                quota_ledger=self.quota,
                                quota_patterns=patterns,
                                env=env,
                            )
                            self.runners[active_runner_name] = runner_obj
                    else:
                        raise RuntimeError(f"No runner available for engine '{active_runner_name}'")

                # Record step in SQLite before execution
                step_id = self.store.record_step(phase.id, "implement", active_runner_name)
                self.scheduler.record_invocation()

                self.events.emit(Event(
                    kind="runner.invoked",
                    plain_text=f"Dispatching {phase.title} attempt {implement_attempts} to {active_runner_name} ({choice.reason}).",
                    phase_id=phase.id,
                ))

                instruction = (
                    "Read and execute every instruction in .nightshift_prompt.md in the current directory. "
                    "Push the branch and open the PR targeting main when done."
                )
                if implement_attempts > 1 and (wt_dir / ".nightshift" / "handoff.md").exists():
                    instruction += (
                        " HANDOFF NOTE: Read .nightshift/handoff.md for prior progress and any findings to fix."
                    )

                # Capture remote SHA BEFORE runner execution for accurate NO-OP detection
                sha_before = self.vcs.remote_branch_sha(project.local_path, phase.branch)
                inv_timeout = self.scheduler.invocation_timeout_seconds(
                    phase.id, active_runner_name, min(phase.timeout_minutes, int(self.store.get_setting("guard.runnerTimeoutMinutes", "90") or "90")) * 60
                )
                res = await runner_obj.run(
                    instruction,
                    wt_dir,
                    model=choice.model or None,
                    effort=choice.effort,
                    timeout_seconds=inv_timeout,
                )

                # Persist redacted raw log as a file (BLUEPRINT 5d)
                logs_dir = project.output_dir / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                raw_log_path = logs_dir / f"{phase.id}_attempt_{implement_attempts}_{active_runner_name}.log"
                raw_log_content = redact_secrets(f"RC={res.rc}\n--- STDOUT ---\n{res.stdout}\n--- STDERR ---\n{res.stderr}\n")
                raw_log_path.write_text(raw_log_content, encoding="utf-8")

                # Verify deliverable (open PR or new commit pushed) & NO-OP check
                ok_deliv, deliv_reason = verify_deliverable(
                    self.vcs,
                    project.repo,
                    phase.branch,
                    previous_sha=sha_before,
                    local_repo=project.local_path,
                )

                # Run diff guards and re-estimate job size from actual diff (BLUEPRINT 5h)
                diff_text = self.vcs.get_diff(wt_dir)
                changed_files = self.vcs.list_changed_files(wt_dir)
                findings = check_diff_guards(wt_dir, changed_files, diff_text)
                effective_size = (
                    self.scheduler.router.estimate_job_size(phase.prompt, changed_files, diff_text)
                    if diff_text
                    else phase.size
                )
                if effective_size != phase.size:
                    self.store.update_phase(phase.id, size=effective_size)

                for f in findings:
                    self.store.record_guard_result(phase.id, f.guard, passed=False, detail=f.message)

                elapsed_min = max(0.01, (time.time() - t0) / 60.0)
                if res.rc == 0 and ok_deliv and not findings:
                    delivered = True
                    self.store.update_step(step_id, rc=0, verified=1, detail=deliv_reason)
                    self.store.record_duration("implement", active_runner_name, time.time() - t0)
                    self.store.update_engine_stats(active_runner_name, effective_size, success=True, minutes=elapsed_min)
                    self.events.emit(Event(
                        kind="runner.completed",
                        plain_text=f"{active_runner_name} delivered {phase.title}: {deliv_reason}",
                        phase_id=phase.id,
                        raw_path=str(raw_log_path),
                    ))
                    break

                # Failure, diff-guard finding, or quota exhaustion -> self-heal / mid-phase handoff
                self.store.update_engine_stats(active_runner_name, effective_size, success=False, minutes=elapsed_min)
                failure_reason = "Diff guard failed: " + "; ".join(f.message for f in findings) if findings else deliv_reason if not ok_deliv else res.stderr[:200]
                self.store.update_step(step_id, rc=res.rc, verified=0, detail=failure_reason)
                if findings:
                    diff_guard_retries += 1
                    findings_summary = "\n".join(f"- [{f.guard}] {f.message}" for f in findings)
                    prompt_path.write_text(
                        guardrail_header + phase.prompt + f"\n\nDIFF GUARD FINDINGS TO FIX:\n{findings_summary}\n",
                        encoding="utf-8",
                    )
                    if diff_guard_retries < 2:
                        pending_choice = choice
                        continue

                # Look for next engine to hand off to
                next_choice = self.scheduler.select_engine(
                    phase_id=phase.id,
                    step_kind="implement",
                    current_engine=active_runner_name,
                    exclude_engines={active_runner_name},
                )
                pending_choice = next_choice
                next_engine = next_choice.engine if next_choice else "none"

                self.scheduler.create_mid_phase_handoff(
                    vcs=self.vcs,
                    worktree_dir=wt_dir,
                    phase_id=phase.id,
                    phase_title=phase.title,
                    from_engine=active_runner_name,
                    to_engine=next_engine,
                    reason=failure_reason,
                    last_error=res.stderr or res.stdout,
                    branch=phase.branch,
                )
                self.events.emit(Event(
                    kind="phase.handoff",
                    plain_text=f"{active_runner_name} exhausted/failed on {phase.title}; handed off to {next_engine}.",
                    phase_id=phase.id,
                    raw_path=str(raw_log_path),
                ))

            if not delivered:
                self._park_phase(project.id, phase, reason="Exhausted implementer attempts without valid delivery", next_step="Manual inspection")
                return {"phase_id": phase.id, "title": phase.title, "runner": active_runner_name, "status": "PARKED", "detail": "Exhausted implementer attempts"}

            pr_info = self.vcs.find_open_pr(project.repo, phase.branch)
            if not pr_info:
                self._park_phase(project.id, phase, reason="No open PR found after implementation delivery", next_step="Open PR manually")
                return {"phase_id": phase.id, "title": phase.title, "runner": active_runner_name, "status": "PARKED", "detail": "No open PR found"}
            pr_number = int(pr_info["number"])
            pr_url = str(pr_info["url"])
            self.store.update_phase(phase.id, pr_number=pr_number)

        # Check resumed PRs before CI and check the final head again after fixes.
        def park_on_diff_guard() -> dict[str, Any] | None:
            findings = check_diff_guards(wt_dir, self.vcs.list_changed_files(wt_dir), self.vcs.get_diff(wt_dir))
            if not findings:
                return None
            for finding in findings:
                self.store.record_guard_result(phase.id, finding.guard, passed=False, detail=finding.message)
            reason = "Diff guard failed: " + "; ".join(f.message for f in findings)
            self._park_phase(project.id, phase, reason=reason, next_step="Fix the diff guard findings")
            return {"phase_id": phase.id, "title": phase.title, "runner": active_runner_name, "status": "PARKED", "detail": reason}

        guard_failure = park_on_diff_guard()
        if guard_failure:
            return guard_failure

        # Step 3: CI Watch and Self-Healing Fix Loop
        self.store.update_phase(phase.id, status="watching_ci")
        diff_text = self.vcs.get_diff(wt_dir)
        changed_files = self.vcs.list_changed_files(wt_dir)
        risk = assess_diff_risk(changed_files, diff_text, critical_paths=project.critical_paths)

        def on_ci_status(s: str) -> None:
            self.store.update_phase(phase.id, status=s)

        ci_res: CiWatchResult = await self.ci_pipeline.watch_and_heal(
            repo=project.repo,
            local_repo=project.local_path,
            worktree_dir=wt_dir,
            phase_id=phase.id,
            phase_title=phase.title,
            branch=phase.branch,
            pr_number=pr_number,
            initial_engine=active_runner_name,
            risk=risk,
            guardrail_header=guardrail_header,
            on_status_change=on_ci_status,
        )

        if not ci_res.passed:
            self._park_phase(project.id, phase, reason=ci_res.detail, next_step="Fix remaining CI failure")
            return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": ci_res.detail}

        guard_failure = park_on_diff_guard()
        if guard_failure:
            return guard_failure

        detail = f"PR #{pr_number} green ({pr_url})"
        if ci_res.flaky:
            detail += " [flaky test resolved]"

        # Step 4: Merge PR if auto_merge is enabled (verify exact head SHA match)
        if phase.auto_merge and not dependencies_merged:
            reason = "Waiting for dependencies to merge before merging this PR"
            self._park_phase(project.id, phase, reason=reason, next_step="Merge dependencies and resume")
            return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": reason}

        if phase.auto_merge:
            latest_pr = self.vcs.find_open_pr(project.repo, phase.branch)
            if (
                not ci_res.checked_sha or not latest_pr or not latest_pr.get("headRefOid")
                or str(latest_pr["headRefOid"]) != ci_res.checked_sha
            ):
                mismatch_reason = (
                    f"Hard guard: PR #{pr_number} head SHA changed after CI passed "
                    f"(checked {str(ci_res.checked_sha)[:7]}, now {str((latest_pr or {}).get('headRefOid'))[:7]})."
                )
                self.store.record_guard_result(phase.id, "exact_head_sha", passed=False, detail=mismatch_reason)
                self._park_phase(project.id, phase, reason=mismatch_reason, next_step="Re-run CI on the latest head commit")
                return {
                    "phase_id": phase.id,
                    "title": phase.title,
                    "runner": ci_res.last_engine,
                    "status": "PARKED",
                    "detail": mismatch_reason,
                }
            self.store.update_phase(phase.id, status="merging")
            self.events.emit(Event(
                kind="phase.merging",
                plain_text=f"Merging PR #{pr_number} cleanly into main.",
                phase_id=phase.id,
                payload={"project_id": project.id},
            ))
            merge_ok, merge_msg = self.vcs.merge_pr(project.repo, pr_number, project.local_path)
            if not merge_ok:
                self._park_phase(project.id, phase, reason=f"Merge failed: {merge_msg}", next_step="Resolve merge conflict or checks")
                return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": f"Merge failed: {merge_msg}"}

            self.store.update_phase(phase.id, status="merged")
            self.events.emit(Event(
                kind="phase.merged",
                plain_text=f"Merged {phase.title} via PR #{pr_number}.",
                level="info",
                phase_id=phase.id,
                payload={"project_id": project.id},
            ))
            detail = f"Merged PR #{pr_number}"

            merged_pr = self.vcs.find_merged_pr(project.repo, phase.branch) or {}
            result = self._finish_merged_delivery(project, phase, merged_pr, ci_res.last_engine)
            if result["status"] != "COMPLETED":
                return result
            detail = result["detail"]

        # Cleanup worktree
        self.vcs.remove_worktree(project.local_path, wt_dir)
        self.store.update_phase(phase.id, status="completed")
        self.events.emit(Event(
            kind="phase.completed",
            plain_text=f"Completed {phase.title}.",
            level="info",
            phase_id=phase.id,
            payload={"project_id": project.id},
        ))

        return {
            "phase_id": phase.id,
            "title": phase.title,
            "runner": ci_res.last_engine,
            "status": "COMPLETED",
            "detail": detail,
        }

    def _finish_merged_delivery(
        self, project: ProjectConfig, phase: PhaseDefinition, merged_pr: dict[str, Any], runner: str
    ) -> dict[str, Any]:
        """Resume post-merge delivery without repeating implementation or merge."""
        self.store.update_phase(phase.id, status="verifying_build", pr_number=merged_pr.get("number"))
        merge_sha = (merged_pr.get("mergeCommit") or {}).get("oid")
        try:
            if not merge_sha:
                raise RuntimeError("Could not determine the merged commit SHA")
            ok, msg, run_id = self.vcs.watch_workflow_run(project.repo, branch="main", workflow=project.release_workflow, timeout_seconds=3600, commit_sha=merge_sha)
            if ok is False:
                reverted, revert_msg = self.ci_pipeline.handle_post_merge_failure(project.local_path, merge_sha, phase.id, msg, project.repo)
                if reverted:
                    return {"phase_id": phase.id, "title": phase.title, "runner": runner, "status": "PARKED", "detail": revert_msg}
                raise RuntimeError(f"Main build failed: {msg}")
            if ok is not True or run_id is None:
                raise RuntimeError(f"Could not verify main build: {msg}")
            if not phase.release_after_merge:
                self.store.unpark_phase(phase.id)
                self.store.update_phase(phase.id, status="completed")
                self.events.emit(Event(
                    kind="phase.completed",
                    plain_text=f"Completed {phase.title}.",
                    level="info",
                    phase_id=phase.id,
                    payload={"project_id": project.id},
                ))
                return {"phase_id": phase.id, "title": phase.title, "runner": runner, "status": "COMPLETED", "detail": f"Merged PR #{merged_pr.get('number')}; main build passed"}
            self.store.update_phase(phase.id, status="releasing")
            tag, files = self.release_mgr.publish_verified_release(
                repo=project.repo, merge_sha=merge_sha, output_dir=project.output_dir,
                title_note=phase.title, workflow_name=project.release_workflow,
                artifact_glob=project.artifact_glob, tag_prefix=project.tag_prefix, run_id=run_id,
            )
            self.store.unpark_phase(phase.id)
            self.store.update_phase(phase.id, status="completed")
            self.events.emit(Event(
                kind="release.published",
                plain_text=f"Published verified release {tag} with SHA-256 checksums.",
                phase_id=phase.id,
                payload={"project_id": project.id},
            ))
            return {"phase_id": phase.id, "title": phase.title, "runner": runner, "status": "COMPLETED", "detail": f"Merged PR #{merged_pr.get('number')} ? Released {tag} ({len(files)} files)"}
        except Exception as rel_err:
            logger.error("Post-merge release failed: %s", rel_err)
            self._park_phase(project.id, phase, reason=str(rel_err), next_step="Inspect the main build or release and resume")
            return {"phase_id": phase.id, "title": phase.title, "runner": runner, "status": "PARKED", "detail": f"Post-merge failure: {rel_err}"}
