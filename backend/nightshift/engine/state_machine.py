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
from nightshift.engine.pipeline_contracts import MergeGate, provider_for_engine
from nightshift.engine.pipeline_evidence import (
    DEFAULT_MIN_BUILD_DURATION_S,
    DEFAULT_MIN_GATE_DURATION_S,
    DEFAULT_NIGHTSHIFT_REQUIRED_JOBS,
    persist_ci_evidence,
    persist_screenshot_evidence,
    validate_ci_evidence,
    validate_screenshot_manifest,
)
from nightshift.engine.pipeline_orchestrator import LeaseHeartbeat, TaskOrchestrator, is_process_alive
from nightshift.engine.planner import PhaseDefinition, get_runnable_phases, resolve_base_branch, validate_plan_dag
from nightshift.engine.release import ReleaseManager
from nightshift.engine.scheduler import Scheduler
from nightshift.engine.vcs import GitGhClient, SubprocessGitGh
from nightshift.engine.verify import assess_diff_risk, check_diff_guards, redact_secrets, verify_deliverable
from nightshift.events import Event, EventBus
from nightshift.optimizer.ab_framework import ABFramework
from nightshift.optimizer.repomix import RepomixAdapter, ensure_worktree_git_excluded
from nightshift.optimizer.tool_manager import ToolManager
from nightshift.quota.ledger import QuotaLedger
from nightshift.quota.usage import parse_cli_usage
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
    required_ci_jobs: list[str] = field(default_factory=lambda: list(DEFAULT_NIGHTSHIFT_REQUIRED_JOBS))
    required_artifacts_by_job: dict[str, list[str]] = field(default_factory=dict)
    min_gate_duration_seconds: float = DEFAULT_MIN_GATE_DURATION_S
    min_build_duration_seconds: float = DEFAULT_MIN_BUILD_DURATION_S
    screenshot_manifest_path: str = ""

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

        raw_req_jobs = data.get("required_ci_jobs")
        if raw_req_jobs is None:
            raw_req_jobs = data.get("required_ci_jobs_json")
        if raw_req_jobs is None or raw_req_jobs == "":
            req_jobs = list(DEFAULT_NIGHTSHIFT_REQUIRED_JOBS)
        elif isinstance(raw_req_jobs, str):
            try:
                parsed_jobs = json.loads(raw_req_jobs)
                req_jobs = [str(j) for j in parsed_jobs] if isinstance(parsed_jobs, list) else list(DEFAULT_NIGHTSHIFT_REQUIRED_JOBS)
            except Exception:
                req_jobs = list(DEFAULT_NIGHTSHIFT_REQUIRED_JOBS)
        else:
            req_jobs = [str(j) for j in raw_req_jobs]

        raw_req_arts = data.get("required_artifacts_by_job")
        if raw_req_arts is None:
            raw_req_arts = data.get("required_artifacts_by_job_json")
        if isinstance(raw_req_arts, str):
            try:
                parsed_arts = json.loads(raw_req_arts)
                req_arts = (
                    {str(k): [str(v) for v in vals] for k, vals in parsed_arts.items() if isinstance(vals, list)}
                    if isinstance(parsed_arts, dict)
                    else {}
                )
            except Exception:
                req_arts = {}
        elif isinstance(raw_req_arts, dict):
            req_arts = {str(k): [str(v) for v in vals] for k, vals in raw_req_arts.items() if isinstance(vals, list)}
        else:
            req_arts = {}

        raw_min_gate = data.get("min_gate_duration_seconds")
        min_gate = float(raw_min_gate) if raw_min_gate is not None else DEFAULT_MIN_GATE_DURATION_S
        raw_min_build = data.get("min_build_duration_seconds")
        min_build = float(raw_min_build) if raw_min_build is not None else DEFAULT_MIN_BUILD_DURATION_S

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
            required_ci_jobs=req_jobs,
            required_artifacts_by_job=req_arts,
            min_gate_duration_seconds=min_gate,
            min_build_duration_seconds=min_build,
            screenshot_manifest_path=str(data.get("screenshot_manifest_path") or ""),
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
                    check = subprocess.run(["tasklist", "/FI", f"PID eq {old_pid}"], capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if str(old_pid) in check.stdout:
                        return False
                else:
                    # POSIX: signal 0 only tests whether the process exists.
                    try:
                        os.kill(old_pid, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        return False
                    else:
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
        merge_gate: MergeGate | None = None,
        lease_seconds: int = 1800,
        lease_heartbeat_interval: float | None = None,
    ) -> None:
        self.store = store
        self.events = event_bus
        self.quota = quota_ledger
        self.vcs = vcs or SubprocessGitGh()
        self.runners = runners or {}
        self.logins = login_checker or LoginHealthChecker(store=store, quota_ledger=quota_ledger)
        self._network_backoff_seconds = network_backoff_seconds
        self._lease_seconds = lease_seconds
        self._lease_heartbeat_interval = lease_heartbeat_interval
        # Single mandatory merge-gate chokepoint (AGENT_PIPELINE §1).
        # Autonomous merge stays OFF by default until Phases 8b-8e reach Integration-tested.
        self.merge_gate = merge_gate or MergeGate(autonomous_merge_enabled=False)
        self.task_orchestrator = TaskOrchestrator(store=store)

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
                    required_ci_jobs_json=json.dumps(project.required_ci_jobs),
                    required_artifacts_by_job_json=json.dumps(project.required_artifacts_by_job),
                    min_gate_duration_seconds=project.min_gate_duration_seconds,
                    min_build_duration_seconds=project.min_build_duration_seconds,
                    screenshot_manifest_path=project.screenshot_manifest_path,
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

            # Crash recovery on startup: WIP-checkpoint & release leases for orphaned RUNNING tasks (AGENT_PIPELINE §4)
            def _crash_checkpoint(orphan_task: dict[str, Any]) -> None:
                orphan_phase = str(orphan_task.get("phase_id") or "")
                orphan_branch = str(orphan_task.get("branch") or "")
                orphan_wt = WORKTREE_ROOT / orphan_phase.replace("/", "_").replace("\\", "_")
                if orphan_branch and orphan_wt.exists():
                    try:
                        self.vcs.create_checkpoint_commit_and_push(
                            orphan_wt,
                            orphan_branch,
                            "nightshift: crash-checkpoint",
                        )
                    except Exception as exc:
                        logger.warning("Crash-checkpoint failed for orphaned task %s: %s", orphan_task.get("id"), exc)

            recovered_tasks = self.task_orchestrator.recover_orphaned_running_tasks(
                is_pid_alive=is_process_alive,
                checkpoint_fn=_crash_checkpoint,
            )
            for recovered_id in recovered_tasks:
                self.events.emit(Event(
                    kind="task.recovered",
                    plain_text=f"Recovered orphaned task {recovered_id} after previous worker exit.",
                    level="warn",
                    payload={"project_id": project.id, "task_id": recovered_id},
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

    def _ensure_pipeline_task(
        self,
        project: ProjectConfig,
        phase: PhaseDefinition,
        plan_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Ensure a durable pipeline task and frozen requirement contract exist for ``phase``."""
        existing = self.store.get_task_by_branch(phase.branch)
        if existing is not None and existing.get("plan_id") == plan_id and existing.get("phase_id") == phase.id:
            contract = self.store.get_task_contract(str(existing["contract_id"]))
            if contract is not None:
                return existing, contract
        task_id = f"task-{phase.id}"
        if self.store.get_task(task_id) is not None:
            existing = self.store.get_task(task_id)
            assert existing is not None
            contract = self.store.get_task_contract(str(existing["contract_id"]))
            if contract is not None:
                return existing, contract
        return self.store.create_task_with_contract(
            task_id=task_id,
            phase_id=phase.id,
            plan_id=plan_id,
            title=phase.title,
            branch=phase.branch,
            risk_class=phase.risk_class,
            allowed_globs=list(phase.allowed_globs),
            forbidden_globs=list(phase.forbidden_globs) if phase.forbidden_globs is not None else None,
            owner_approval_required=phase.owner_approval_required,
        )

    def _safe_task_transition(
        self,
        task_id: str,
        to_status: str,
        *,
        idempotency_key: str,
        reason: str,
        actor: str = "engine",
        updates: dict[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any] | None:
        """Advance ``task_id`` through ``TaskOrchestrator`` when the transition is legal from its current state."""
        cur = self.store.get_task(task_id)
        if cur is None or cur["status"] == to_status:
            return cur
        return self.task_orchestrator.transition(
            task_id,
            to_status,
            idempotency_key=idempotency_key,
            reason=reason,
            actor=actor,
            updates=updates,
            lease_token=lease_token,
        )

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

        # Ensure a durable pipeline task + frozen requirement contract exists before coding/merging (AGENT_PIPELINE §3.1)
        pipeline_task, pipeline_contract = self._ensure_pipeline_task(project, phase, plan_id)
        task_id = str(pipeline_task["id"])
        lease_token = self.task_orchestrator.acquire_lease(task_id, lease_seconds=self._lease_seconds)
        try:
            with LeaseHeartbeat(
                self.task_orchestrator,
                task_id,
                lease_token,
                lease_seconds=self._lease_seconds,
                interval_seconds=self._lease_heartbeat_interval,
            ):
                return await self._execute_phase_leased(
                    project=project,
                    phase=phase,
                    plan_id=plan_id,
                    base_branch=base_branch,
                    dependencies_merged=dependencies_merged,
                    wt_dir=wt_dir,
                    pipeline_task=pipeline_task,
                    pipeline_contract=pipeline_contract,
                    lease_token=lease_token,
                )
        finally:
            self.task_orchestrator.release_lease(task_id, lease_token)

    async def _execute_phase_leased(
        self,
        *,
        project: ProjectConfig,
        phase: PhaseDefinition,
        plan_id: str,
        base_branch: str,
        dependencies_merged: bool,
        wt_dir: Path,
        pipeline_task: dict[str, Any],
        pipeline_contract: dict[str, Any],
        lease_token: str,
    ) -> dict[str, Any]:
        """Execute a single phase while holding a fenced task lease."""
        task_id = str(pipeline_task["id"])
        active_runner_name = "unknown"
        t0 = time.time()
        contract_allowed_globs = json.loads(str(pipeline_contract.get("allowed_globs_json") or "[]"))
        contract_forbidden_globs = json.loads(str(pipeline_contract.get("forbidden_globs_json") or "[]"))

        guardrail_header = (
            "CONTINUOUS ORCHESTRATOR GUARDRAILS:\n"
            f"- Isolated worktree on branch `{phase.branch}`.\n"
            f"- Zero glitches: ensure clean compilation, strict tests, and zero regressions.\n"
            f"- Commit cleanly, push `{phase.branch}`, and open a PR targeting `main` (if PR exists, push to it).\n"
            "- NEVER merge to `main` or cut releases yourself; the orchestrator merges after CI passes.\n\n"
        )

        if project.ui_contract:
            guardrail_header += f"{project.ui_contract}\n\n"

        # Step 2: Reconcile PR state via durable outbox before implementation (AGENT_PIPELINE §4)
        def _active_prs_for_branch() -> list[dict[str, Any]]:
            prs = self.vcs.list_prs_for_branch(project.repo, phase.branch)
            filtered: list[dict[str, Any]] = []
            for pr in prs:
                if str(pr.get("state", "")).upper() == "MERGED":
                    m_sha = str((pr.get("mergeCommit") or {}).get("oid") or "")
                    if m_sha and self.vcs.find_merged_pr(project.repo, f"nightshift/revert-{m_sha}"):
                        continue
                filtered.append(pr)
            return filtered

        pr_reconcile = self.task_orchestrator.reconcile_pr_create(
            task_id=task_id,
            branch=phase.branch,
            lease_token=lease_token,
            pr_list_lookup=_active_prs_for_branch,
        )
        pr_number: int | None = None
        pr_url: str | None = None

        if pr_reconcile.action == "blocked":
            block_reason = str(pr_reconcile.data.get("block_reason") or "pr_reconcile_blocked")
            self._safe_task_transition(
                task_id,
                "BLOCKED",
                idempotency_key=f"pr-reconcile-blocked:{task_id}:{block_reason}:{int(t0)}",
                reason=pr_reconcile.detail,
                updates={"block_reason": block_reason, "block_detail": pr_reconcile.detail},
                lease_token=lease_token,
            )
            self._park_phase(project.id, phase, reason=pr_reconcile.detail, next_step="Inspect existing PR state on GitHub")
            return {"phase_id": phase.id, "title": phase.title, "runner": "none", "status": "PARKED", "detail": pr_reconcile.detail}

        if pr_reconcile.action == "adopted_merged_pr":
            merged_pr = dict(pr_reconcile.data)
            return self._finish_merged_delivery(project, phase, merged_pr, "none", lease_token=lease_token)

        existing_pr = pr_reconcile.data if pr_reconcile.action == "adopted_open_pr" else self.vcs.find_open_pr(project.repo, phase.branch)

        if existing_pr:
            pr_number = int(existing_pr["number"])
            pr_url = str(existing_pr["url"])
            cur_task = self.store.get_task(task_id)
            if cur_task and cur_task["status"] == "QUEUED":
                self._safe_task_transition(
                    task_id,
                    "RUNNING",
                    idempotency_key=f"adopt-open-pr-run:{task_id}:{pr_number}",
                    reason=f"Adopting open PR #{pr_number}",
                    lease_token=lease_token,
                )
            cur_task = self.store.get_task(task_id)
            if cur_task and cur_task["status"] == "RUNNING":
                self._safe_task_transition(
                    task_id,
                    "VERIFYING",
                    idempotency_key=f"adopt-open-pr-verify:{task_id}:{pr_number}",
                    reason=f"Resumed open PR #{pr_number} at CI watch",
                    updates={"pr_number": pr_number, "head_sha": existing_pr.get("headRefOid")},
                    lease_token=lease_token,
                )
            self.store.update_phase(phase.id, status="watching_ci", pr_number=pr_number)
            self.events.emit(Event(
                kind="phase.resumed",
                plain_text=f"PR #{pr_number} already open for {phase.branch} -> skipping implementation, resuming at CI watch.",
                phase_id=phase.id,
            ))
        else:
            # Implementation ladder loop with mid-phase handoff & fast switch-back
            cur_task = self.store.get_task(task_id)
            if cur_task and cur_task["status"] == "QUEUED":
                self._safe_task_transition(
                    task_id,
                    "RUNNING",
                    idempotency_key=f"start-running:{task_id}:{int(t0)}",
                    reason=f"Lease acquired and worktree ready for {phase.title}",
                    lease_token=lease_token,
                )
            self.store.update_phase(phase.id, status="implementing")
            self.events.emit(Event(
                kind="phase.implementing",
                plain_text=f"Starting implementation of {phase.title}.",
                phase_id=phase.id,
            ))

            prompt_path = wt_dir / ".nightshift_prompt.md"
            prompt_body = guardrail_header + phase.prompt
            if (wt_dir / ".git").exists():
                ensure_worktree_git_excluded(wt_dir)
            repomix_state = self.store.get_token_tool_state("repomix")
            if repomix_state and bool(repomix_state.get("enabled", 0)):
                tm = ToolManager(store=self.store)
                rm_res = RepomixAdapter(tm).generate_repo_map(wt_dir)
                if rm_res.ok and rm_res.content:
                    prompt_body = RepomixAdapter.inject_into_prompt(prompt_body, rm_res.content)
                elif rm_res.warning:
                    self.events.emit(Event(
                        kind="token_optimizer.warning",
                        plain_text=rm_res.warning,
                        level="warn",
                        phase_id=phase.id,
                    ))
            prompt_path.write_text(prompt_body, encoding="utf-8")

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
                self.store.record_task_contributing_provider(
                    str(pipeline_task["id"]),
                    provider_for_engine(active_runner_name),
                )

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

                # Record A/B samples for enabled token optimizer tools (BLUEPRINT 5g)
                # Parse token usage from runner output
                raw_output = res.stdout if res.rc == 0 else res.stderr
                tokens_in, tokens_out = parse_cli_usage(raw_output)
                turns = 1
                retry_turns = 0
                success = res.rc == 0

                # Record A/B samples for all enabled tools
                from nightshift.optimizer.tool_manager import ToolManager
                tm = ToolManager(store=self.store)
                enabled_tools = []
                for spec in tm.list_specs():
                    if spec.catalogue_only:
                        continue
                    db_state = self.store.get_token_tool_state(spec.name)
                    if db_state and bool(db_state.get("enabled", 0)):
                        enabled_tools.append(spec.name)

                if enabled_tools:
                    for tool_name in enabled_tools:
                        self.store.record_token_ab_run(
                            tool=tool_name,
                            enabled=True,
                            phase_kind=phase.ladder if phase.ladder else "default",
                            engine=active_runner_name,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            turns=turns,
                            retry_turns=retry_turns,
                            success=success,
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

                # Run diff guards and re-estimate job size from actual diff (BLUEPRINT 5h, AGENT_PIPELINE §3)
                diff_text = self.vcs.get_diff(wt_dir)
                changed_files = self.vcs.list_changed_files(wt_dir)
                diff_entries = self.vcs.list_changed_entries(wt_dir)
                test_snapshots = self.vcs.get_test_file_snapshots(wt_dir, diff_entries)
                findings = check_diff_guards(
                    wt_dir,
                    changed_files,
                    diff_text,
                    diff_entries=diff_entries,
                    allowed_globs=contract_allowed_globs,
                    forbidden_globs=contract_forbidden_globs,
                    test_file_snapshots=test_snapshots,
                )
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

                    # Evaluate A/B results for enabled tools (BLUEPRINT 5g)
                    ab = ABFramework(self.store, self.events)
                    tm = ToolManager(store=self.store)
                    for spec in tm.list_specs():
                        if spec.catalogue_only:
                            continue
                        db_state = self.store.get_token_tool_state(spec.name)
                        if db_state and bool(db_state.get("enabled", 0)):
                            ab.evaluate_and_apply(spec.name, phase.ladder if phase.ladder else "default")

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
            cur_task = self.store.get_task(task_id)
            if cur_task and cur_task["status"] == "RUNNING":
                self._safe_task_transition(
                    task_id,
                    "VERIFYING",
                    idempotency_key=f"delivered-pr:{task_id}:{pr_number}:{pr_info.get('headRefOid') or ''}",
                    reason=f"Verified deliverable PR #{pr_number}",
                    updates={"pr_number": pr_number, "head_sha": pr_info.get("headRefOid")},
                    lease_token=lease_token,
                )

        # Check resumed PRs before CI and check the final head again after fixes.
        def park_on_diff_guard() -> dict[str, Any] | None:
            cur_diff = self.vcs.get_diff(wt_dir)
            cur_files = self.vcs.list_changed_files(wt_dir)
            cur_entries = self.vcs.list_changed_entries(wt_dir)
            cur_snapshots = self.vcs.get_test_file_snapshots(wt_dir, cur_entries)
            findings = check_diff_guards(
                wt_dir,
                cur_files,
                cur_diff,
                diff_entries=cur_entries,
                allowed_globs=contract_allowed_globs,
                forbidden_globs=contract_forbidden_globs,
                test_file_snapshots=cur_snapshots,
            )
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

        for fixer_engine in ci_res.contributing_engines:
            self.store.record_task_contributing_provider(
                str(pipeline_task["id"]),
                provider_for_engine(fixer_engine),
            )

        if not ci_res.passed:
            self._park_phase(project.id, phase, reason=ci_res.detail, next_step="Fix remaining CI failure")
            return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": ci_res.detail}

        # Step 3b: Authoritative CI & screenshot evidence integrity check (Amendment F / Phase 8d)
        expected_base_sha = ci_res.expected_base_sha or self.vcs.remote_branch_sha(project.local_path, "main")
        current_base_sha = self.vcs.remote_branch_sha(project.local_path, "main")
        if ci_res.checked_sha:
            observed_jobs = (
                list(ci_res.ci_jobs)
                if ci_res.ci_jobs
                else self.vcs.get_commit_ci_checks(project.repo, ci_res.checked_sha, branch=phase.branch)
            )
            ci_ev = validate_ci_evidence(
                expected_commit_sha=ci_res.checked_sha,
                required_jobs=project.required_ci_jobs,
                observed_jobs=observed_jobs,
                required_artifacts_by_job=project.required_artifacts_by_job,
                expected_base_sha=expected_base_sha,
                current_base_sha=current_base_sha,
                min_gate_duration_s=project.min_gate_duration_seconds,
                min_build_duration_s=project.min_build_duration_seconds,
            )
            persist_ci_evidence(
                self.store,
                task_id=task_id,
                contract_sha256=str(pipeline_contract["contract_sha256"]),
                commit_sha=ci_res.checked_sha,
                kind="ci_check",
                observed_jobs=observed_jobs,
                validation=ci_ev,
            )
            if not ci_ev.verified:
                ev_reason = f"CI evidence rejected ({ci_ev.rejection_reason}): {'; '.join(ci_ev.details)}"
                self.store.record_guard_result(phase.id, f"evidence:{ci_ev.rejection_reason}", passed=False, detail=ev_reason)
                self._safe_task_transition(
                    task_id,
                    "BLOCKED",
                    idempotency_key=f"ci-evidence-rejected:{task_id}:{ci_res.checked_sha}:{ci_ev.rejection_reason}",
                    reason=ev_reason,
                    updates={
                        "blocked_from_status": "VERIFYING",
                        "head_sha": ci_res.checked_sha,
                        "expected_base_sha": expected_base_sha,
                        "block_reason": ci_ev.rejection_reason,
                        "block_detail": ev_reason,
                        "next_step": "Re-run real CI checks on the exact head SHA with required artifacts",
                    },
                    lease_token=lease_token,
                )
                self._park_phase(
                    project.id,
                    phase,
                    reason=ev_reason,
                    next_step="Re-run real CI checks on the exact head SHA with required artifacts",
                )
                return {
                    "phase_id": phase.id,
                    "title": phase.title,
                    "runner": ci_res.last_engine,
                    "status": "PARKED",
                    "detail": ev_reason,
                }

            if project.screenshot_manifest_path:
                manifest_p = Path(project.screenshot_manifest_path)
                frontend_job = next(
                    (
                        j
                        for j in observed_jobs
                        if j.name == "desktop-frontend" and j.commit_sha == ci_res.checked_sha and j.build_id
                    ),
                    None,
                )
                expected_shot_build = frontend_job.build_id if frontend_job is not None else None
                shot_ev = validate_screenshot_manifest(
                    manifest_p,
                    expected_commit_sha=ci_res.checked_sha,
                    expected_build_id=expected_shot_build,
                )
                persist_screenshot_evidence(
                    self.store,
                    task_id=task_id,
                    contract_sha256=str(pipeline_contract["contract_sha256"]),
                    commit_sha=ci_res.checked_sha,
                    manifest_path=manifest_p,
                    validation=shot_ev,
                    build_id=expected_shot_build or "",
                )
                if not shot_ev.verified:
                    shot_reason = f"Screenshot manifest rejected ({shot_ev.rejection_reason}): {'; '.join(shot_ev.details)}"
                    self.store.record_guard_result(phase.id, f"screenshot:{shot_ev.rejection_reason}", passed=False, detail=shot_reason)
                    self._safe_task_transition(
                        task_id,
                        "BLOCKED",
                        idempotency_key=f"screenshot-rejected:{task_id}:{ci_res.checked_sha}:{shot_ev.rejection_reason}",
                        reason=shot_reason,
                        updates={
                            "blocked_from_status": "VERIFYING",
                            "head_sha": ci_res.checked_sha,
                            "expected_base_sha": expected_base_sha,
                            "block_reason": shot_ev.rejection_reason,
                            "block_detail": shot_reason,
                            "next_step": "Regenerate verified screenshots and screenshot-manifest.json on CI",
                        },
                        lease_token=lease_token,
                    )
                    self._park_phase(
                        project.id,
                        phase,
                        reason=shot_reason,
                        next_step="Regenerate verified screenshots and screenshot-manifest.json on CI",
                    )
                    return {
                        "phase_id": phase.id,
                        "title": phase.title,
                        "runner": ci_res.last_engine,
                        "status": "PARKED",
                        "detail": shot_reason,
                    }

            task_updates: dict[str, Any] = {
                "head_sha": ci_res.checked_sha,
                "pr_number": pr_number,
                "expected_base_sha": expected_base_sha,
            }
            if ci_res.reviewer_provider:
                task_updates["review_sha"] = ci_res.checked_sha
            cur_task = self.store.get_task(task_id)
            if cur_task and cur_task["status"] == "VERIFYING":
                self._safe_task_transition(
                    task_id,
                    "REVIEWING",
                    idempotency_key=f"ci-verified:{task_id}:{ci_res.checked_sha}",
                    reason=f"CI and diff guards passed on {ci_res.checked_sha[:7]}",
                    updates=task_updates,
                    lease_token=lease_token,
                )
            else:
                self.store.update_task_fields(task_id, **task_updates)

        guard_failure = park_on_diff_guard()
        if guard_failure:
            return guard_failure

        detail = f"PR #{pr_number} green ({pr_url})"
        if ci_res.flaky:
            detail += " [flaky test resolved]"

        # Step 4: Merge PR if auto_merge is enabled (verify exact head SHA match + single MergeGate chokepoint)
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

            # Mandatory single MergeGate chokepoint (AGENT_PIPELINE §1, §3)
            fresh_task = self.store.get_task(str(pipeline_task["id"])) or pipeline_task
            fresh_contract = self.store.get_task_contract(str(fresh_task["contract_id"])) or pipeline_contract
            final_diff = self.vcs.get_diff(wt_dir)
            final_entries = self.vcs.list_changed_entries(wt_dir)
            final_snapshots = self.vcs.get_test_file_snapshots(wt_dir, final_entries)
            auth = self.merge_gate.authorize_merge(
                task=fresh_task,
                contract=fresh_contract,
                head_sha=ci_res.checked_sha,
                expected_base_sha=fresh_task.get("expected_base_sha") or expected_base_sha,
                current_base_sha=self.vcs.remote_branch_sha(project.local_path, "main"),
                diff_entries=final_entries,
                diff_content=final_diff,
                test_file_snapshots=final_snapshots,
                evidence_records=self.store.list_evidence_records(task_id, commit_sha=ci_res.checked_sha),
                required_ci_jobs=project.required_ci_jobs,
                require_screenshot_evidence=bool(project.screenshot_manifest_path),
                reviewer_provider=ci_res.reviewer_provider or None,
            )
            if not auth.allowed:
                self.store.record_guard_result(
                    phase.id,
                    f"merge_gate:{auth.block_reason or 'blocked'}",
                    passed=False,
                    detail=auth.reason,
                )
                next_action = (
                    "Owner review and approval required before merge"
                    if auth.block_reason == "awaiting_owner_approval"
                    else "Resolve MergeGate finding and re-verify"
                )
                cur_task = self.store.get_task(task_id)
                if cur_task and cur_task["status"] != "BLOCKED":
                    self._safe_task_transition(
                        task_id,
                        "BLOCKED",
                        idempotency_key=f"merge-gate-blocked:{task_id}:{ci_res.checked_sha}:{auth.block_reason}",
                        reason=auth.reason,
                        updates={
                            "blocked_from_status": "REVIEWING",
                            "head_sha": ci_res.checked_sha,
                            "block_reason": auth.block_reason,
                            "block_detail": auth.reason,
                            "next_step": next_action,
                        },
                        lease_token=lease_token,
                    )
                else:
                    self.store.update_task_fields(
                        task_id,
                        status="BLOCKED",
                        blocked_from_status="REVIEWING",
                        head_sha=ci_res.checked_sha,
                        block_reason=auth.block_reason,
                        block_detail=auth.reason,
                        next_step=next_action,
                    )
                self._park_phase(project.id, phase, reason=auth.reason, next_step=next_action)
                return {
                    "phase_id": phase.id,
                    "title": phase.title,
                    "runner": ci_res.last_engine,
                    "status": "PARKED",
                    "detail": auth.reason,
                }

            self.store.record_guard_result(phase.id, "merge_gate", passed=True, detail=auth.reason)
            cur_task = self.store.get_task(task_id)
            if cur_task and cur_task["status"] == "REVIEWING":
                self._safe_task_transition(
                    task_id,
                    "APPROVED",
                    idempotency_key=f"merge-approved:{task_id}:{ci_res.checked_sha}",
                    reason=auth.reason,
                    updates={"approved_sha": ci_res.checked_sha},
                    lease_token=lease_token,
                )

            # Durable merge outbox reconciliation (AGENT_PIPELINE §4)
            merge_reconcile = self.task_orchestrator.reconcile_merge_outcome(
                task_id=task_id,
                pr_number=pr_number,
                approved_sha=ci_res.checked_sha,
                lease_token=lease_token,
                pr_state_lookup=lambda: self.vcs.get_pr_state(project.repo, pr_number),
                expected_base_sha=fresh_task.get("expected_base_sha"),
                current_base_sha=self.vcs.remote_branch_sha(project.local_path, "main"),
            )
            if merge_reconcile.action == "base_moved":
                self._safe_task_transition(
                    task_id,
                    "VERIFYING",
                    idempotency_key=f"base-moved:{task_id}:{ci_res.checked_sha}:{merge_reconcile.data.get('current_base_sha', '')}",
                    reason=merge_reconcile.detail,
                    lease_token=lease_token,
                )
                self._park_phase(project.id, phase, reason=merge_reconcile.detail, next_step="Update branch from main and re-verify")
                return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": merge_reconcile.detail}

            if merge_reconcile.action == "blocked":
                block_reason = str(merge_reconcile.data.get("block_reason") or "ambiguous_remote_operation")
                self._safe_task_transition(
                    task_id,
                    "BLOCKED",
                    idempotency_key=f"merge-reconcile-blocked:{task_id}:{ci_res.checked_sha}:{block_reason}",
                    reason=merge_reconcile.detail,
                    updates={"block_reason": block_reason, "block_detail": merge_reconcile.detail},
                    lease_token=lease_token,
                )
                self._park_phase(project.id, phase, reason=merge_reconcile.detail, next_step="Reconcile PR merge state manually")
                return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": merge_reconcile.detail}

            self.store.update_phase(phase.id, status="merging")
            merge_sha_adopted = str(merge_reconcile.data.get("merge_sha") or "")
            if merge_reconcile.action != "adopted_merged_pr":
                op_key = str(merge_reconcile.data.get("op_key") or f"pr_merge:{task_id}:{pr_number}:{ci_res.checked_sha}")
                self.events.emit(Event(
                    kind="phase.merging",
                    plain_text=f"Merging PR #{pr_number} cleanly into main.",
                    phase_id=phase.id,
                    payload={"project_id": project.id},
                ))
                try:
                    merge_ok, merge_msg = self.vcs.merge_pr(
                        project.repo,
                        pr_number,
                        project.local_path,
                        match_head_commit=ci_res.checked_sha,
                    )
                except Exception as merge_exc:
                    merge_ok, merge_msg = False, str(merge_exc)

                if not merge_ok:
                    # Reconcile after error/timeout before declaring failure (fail closed if ambiguous)
                    post_reconcile = self.task_orchestrator.reconcile_merge_outcome(
                        task_id=task_id,
                        pr_number=pr_number,
                        approved_sha=ci_res.checked_sha,
                        lease_token=lease_token,
                        pr_state_lookup=lambda: self.vcs.get_pr_state(project.repo, pr_number),
                    )
                    if post_reconcile.action == "adopted_merged_pr":
                        merge_sha_adopted = str(post_reconcile.data.get("merge_sha") or "")
                    else:
                        if post_reconcile.action != "blocked":
                            self.task_orchestrator.complete_remote_operation(
                                op_key,
                                status="failed",
                                result={"error": merge_msg},
                                lease_token=lease_token,
                            )
                        self._safe_task_transition(
                            task_id,
                            "BLOCKED",
                            idempotency_key=f"merge-failed:{task_id}:{ci_res.checked_sha}",
                            reason=f"Merge failed: {merge_msg}",
                            updates={"block_reason": str(post_reconcile.data.get("block_reason") or "merge_failed"), "block_detail": merge_msg},
                            lease_token=lease_token,
                        )
                        self._park_phase(project.id, phase, reason=f"Merge failed: {merge_msg}", next_step="Resolve merge conflict or checks")
                        return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": f"Merge failed: {merge_msg}"}
                else:
                    merged_info = self.vcs.find_merged_pr(project.repo, phase.branch) or {}
                    merge_sha_adopted = str((merged_info.get("mergeCommit") or {}).get("oid") or "")
                    self.task_orchestrator.complete_remote_operation(
                        op_key,
                        status="succeeded",
                        result={"merge_sha": merge_sha_adopted},
                        lease_token=lease_token,
                    )

            merged_at_now = datetime.now(timezone.utc).isoformat()
            cur_task = self.store.get_task(task_id)
            if cur_task and cur_task["status"] == "APPROVED":
                self._safe_task_transition(
                    task_id,
                    "MERGED",
                    idempotency_key=f"merged:{task_id}:{pr_number}:{merge_sha_adopted or ci_res.checked_sha}",
                    reason=f"Merged PR #{pr_number}",
                    updates={
                        "approved_sha": ci_res.checked_sha,
                        "merge_sha": merge_sha_adopted or None,
                        "merged_at": merged_at_now,
                    },
                    lease_token=lease_token,
                )
            else:
                self.store.update_task_fields(
                    task_id,
                    status="MERGED",
                    approved_sha=ci_res.checked_sha,
                    merge_sha=merge_sha_adopted or None,
                    merged_at=merged_at_now,
                )
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
            result = self._finish_merged_delivery(project, phase, merged_pr, ci_res.last_engine, lease_token=lease_token)
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
        self,
        project: ProjectConfig,
        phase: PhaseDefinition,
        merged_pr: dict[str, Any],
        runner: str,
        *,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        """Resume post-merge delivery without repeating implementation, merge, or release."""
        self.store.update_phase(phase.id, status="verifying_build", pr_number=merged_pr.get("number"))
        merge_sha = (merged_pr.get("mergeCommit") or {}).get("oid")
        task = self.store.get_task_by_branch(phase.branch) or self.store.get_task(f"task-{phase.id}")
        acquired_temp_lease: str | None = None
        temp_heartbeat: LeaseHeartbeat | None = None
        rel_work_tmp: Path | None = None
        try:
            if not merge_sha:
                raise RuntimeError("Could not determine the merged commit SHA")

            effective_lease = lease_token
            if task is not None and not effective_lease:
                t_id = str(task["id"])
                acquired_temp_lease = self.task_orchestrator.acquire_lease(
                    t_id,
                    lease_seconds=self._lease_seconds,
                )
                effective_lease = acquired_temp_lease
                temp_heartbeat = LeaseHeartbeat(
                    self.task_orchestrator,
                    t_id,
                    acquired_temp_lease,
                    lease_seconds=self._lease_seconds,
                    interval_seconds=self._lease_heartbeat_interval,
                ).start()

            ok, msg, run_id = self.vcs.watch_workflow_run(project.repo, branch="main", workflow=project.release_workflow, timeout_seconds=3600, commit_sha=merge_sha)
            if ok is False:
                reverted, revert_msg = self.ci_pipeline.handle_post_merge_failure(project.local_path, merge_sha, phase.id, msg, project.repo)
                if reverted:
                    return {"phase_id": phase.id, "title": phase.title, "runner": runner, "status": "PARKED", "detail": revert_msg}
                raise RuntimeError(f"Main build failed: {msg}")
            if ok is not True or run_id is None:
                raise RuntimeError(f"Could not verify main build: {msg}")

            # Post-merge exact merge_sha CI evidence verification (Amendment F / Phase 8d)
            post_jobs = self.vcs.get_commit_ci_checks(project.repo, str(merge_sha), branch="main")
            post_ev = validate_ci_evidence(
                expected_commit_sha=str(merge_sha),
                required_jobs=project.required_ci_jobs,
                observed_jobs=post_jobs,
                required_artifacts_by_job=project.required_artifacts_by_job,
                expected_branch="main",
                min_gate_duration_s=project.min_gate_duration_seconds,
                min_build_duration_s=project.min_build_duration_seconds,
            )
            if task is not None:
                persist_ci_evidence(
                    self.store,
                    task_id=str(task["id"]),
                    contract_sha256=str(task["contract_sha256"]),
                    commit_sha=str(merge_sha),
                    kind="post_merge_ci",
                    observed_jobs=post_jobs,
                    validation=post_ev,
                )
            if not post_ev.verified:
                post_reason = (
                    f"Post-merge CI evidence rejected on {str(merge_sha)[:7]} "
                    f"({post_ev.rejection_reason}): {'; '.join(post_ev.details)}"
                )
                self.store.record_guard_result(
                    phase.id,
                    f"post_merge_evidence:{post_ev.rejection_reason}",
                    passed=False,
                    detail=post_reason,
                )
                if task is not None and effective_lease:
                    self._safe_task_transition(
                        str(task["id"]),
                        "BLOCKED",
                        idempotency_key=f"post-merge-evidence-rejected:{task['id']}:{merge_sha}:{post_ev.rejection_reason}",
                        reason=post_reason,
                        updates={
                            "blocked_from_status": "MERGED",
                            "merge_sha": str(merge_sha),
                            "block_reason": post_ev.rejection_reason,
                            "block_detail": post_reason,
                            "next_step": "Verify full post-merge CI run on exact merge_sha before releasing",
                        },
                        lease_token=effective_lease,
                    )
                self._park_phase(
                    project.id,
                    phase,
                    reason=post_reason,
                    next_step="Verify full post-merge CI run on exact merge_sha before releasing",
                )
                return {
                    "phase_id": phase.id,
                    "title": phase.title,
                    "runner": runner,
                    "status": "PARKED",
                    "detail": post_reason,
                }

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

            # Durable release reconciliation via remote_operations outbox (AGENT_PIPELINE §4)
            rel_op_key: str | None = None
            asset_op_key: str | None = None
            prepared_artifacts: list[Path] | None = None
            if task is not None:
                t_id = str(task["id"])
                assert effective_lease is not None
                # Derive exact required artifact names and SHA-256 checksums from the verified workflow output
                # before reconciling or adopting any existing GitHub release.
                rel_work_tmp = project.output_dir / "temp_download"
                prepared_artifacts, expected_checksums = self.release_mgr.prepare_verified_artifacts(
                    project.repo,
                    run_id,
                    rel_work_tmp,
                    artifact_glob=project.artifact_glob,
                )
                expected_globs = [project.artifact_glob] if project.artifact_glob else []
                rel_reconcile = self.task_orchestrator.reconcile_release_for_merge_sha(
                    task_id=t_id,
                    merge_sha=str(merge_sha),
                    expected_asset_names=list(expected_checksums.keys()),
                    expected_checksums=expected_checksums,
                    expected_asset_globs=expected_globs,
                    lease_token=effective_lease,
                    release_list_lookup=lambda: self.vcs.list_releases(project.repo),
                )
                if rel_reconcile.action == "adopted_release":
                    adopted_tag = str(rel_reconcile.data.get("tag") or "")
                    self.store.update_task_fields(t_id, release_tag=adopted_tag)
                    self.store.unpark_phase(phase.id)
                    self.store.update_phase(phase.id, status="completed")
                    self.events.emit(Event(
                        kind="release.adopted",
                        plain_text=f"Adopted existing verified release {adopted_tag} for {str(merge_sha)[:7]}.",
                        phase_id=phase.id,
                        payload={"project_id": project.id},
                    ))
                    return {"phase_id": phase.id, "title": phase.title, "runner": runner, "status": "COMPLETED", "detail": f"Merged PR #{merged_pr.get('number')} -> Adopted release {adopted_tag}"}
                if rel_reconcile.action == "blocked":
                    block_reason = str(rel_reconcile.data.get("block_reason") or "partial_release_repair")
                    self._safe_task_transition(
                        t_id,
                        "BLOCKED",
                        idempotency_key=f"release-blocked:{t_id}:{merge_sha}:{block_reason}",
                        reason=rel_reconcile.detail,
                        updates={"block_reason": block_reason, "block_detail": rel_reconcile.detail},
                        lease_token=effective_lease,
                    )
                    self._park_phase(project.id, phase, reason=rel_reconcile.detail, next_step="Repair partial release with a fresh incremented tag")
                    return {"phase_id": phase.id, "title": phase.title, "runner": runner, "status": "PARKED", "detail": rel_reconcile.detail}
                rel_op_key = str(rel_reconcile.data.get("op_key") or "")
                asset_op_key = str(rel_reconcile.data.get("asset_op_key") or "")

            publish_kwargs: dict[str, Any] = {
                "repo": project.repo,
                "merge_sha": merge_sha,
                "output_dir": project.output_dir,
                "title_note": phase.title,
                "workflow_name": project.release_workflow,
                "artifact_glob": project.artifact_glob,
                "tag_prefix": project.tag_prefix,
                "run_id": run_id,
            }
            if prepared_artifacts is not None:
                publish_kwargs["prepared_artifacts"] = prepared_artifacts
                publish_kwargs["temp_dir"] = rel_work_tmp
            tag, files = self.release_mgr.publish_verified_release(**publish_kwargs)
            if task is not None and rel_op_key and asset_op_key:
                self.task_orchestrator.complete_remote_operation(
                    rel_op_key,
                    status="succeeded",
                    result={"tag": tag},
                    lease_token=effective_lease,
                )
                self.task_orchestrator.complete_remote_operation(
                    asset_op_key,
                    status="succeeded",
                    result={"tag": tag, "files": [f.name for f in files]},
                    lease_token=effective_lease,
                )
                self.store.update_task_fields(str(task["id"]), release_tag=tag)
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
        finally:
            if rel_work_tmp is not None and rel_work_tmp.exists():
                shutil.rmtree(rel_work_tmp, ignore_errors=True)
            if temp_heartbeat is not None:
                temp_heartbeat.stop()
            if task is not None and acquired_temp_lease:
                self.task_orchestrator.release_lease(str(task["id"]), acquired_temp_lease)
