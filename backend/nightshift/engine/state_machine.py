"""Durable phase state machine with checkpointing, handoff, and recovery (BLUEPRINT 5, 5c, 5d)."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
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
        self.lock_file = base_dir / f"{project_id}.lock"
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

            # Persist plan in store
            self.store.create_plan(plan_id, project.id, f"Run for {project.name}")
            for p in ordered_phases:
                self.store.upsert_phase(
                    p.id,
                    plan_id,
                    p.title,
                    p.branch,
                    prompt=p.prompt,
                    depends_on_json=json.dumps(p.depends_on),
                    size=p.size,
                    position=p.position,
                )

            completed_ids: set[str] = set()
            parked_ids: set[str] = set()

            for phase in ordered_phases:
                # 1. Honour Stop-after-current-phase at boundary
                if self._stop_requested:
                    self.events.emit(Event(
                        kind="run.stopped",
                        plain_text="Stop after current phase requested. Halting cleanly at phase boundary.",
                        level="info",
                        phase_id=phase.id,
                    ))
                    break

                # 2. Check circuit breaker (3 parked phases in a row -> stop starting new phases)
                if self.scheduler.circuit_breaker.is_tripped:
                    self.events.emit(Event(
                        kind="circuit_breaker.tripped",
                        plain_text="Circuit breaker tripped after 3 consecutive parked phases. Pausing new phase starts.",
                        level="warn",
                        phase_id=phase.id,
                    ))
                    break

                # 3. Skip already merged phases unless force_rerun
                if not phase.force_rerun:
                    merged_pr = self.vcs.find_merged_pr(project.repo, phase.branch)
                    if merged_pr:
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
                        self.store.park_phase(phase.id, reason=f"Pre-flight failed: {pf_msg}", next_step="Fix pre-flight issue")
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
                row = await self._execute_phase(project, phase, plan_id, base_branch)
                summary_rows.append(row)

                if row["status"] in ("COMPLETED", "MERGED"):
                    completed_ids.add(phase.id)
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

        return summary_rows

    async def _execute_phase(
        self,
        project: ProjectConfig,
        phase: PhaseDefinition,
        plan_id: str,
        base_branch: str,
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
        ))

        try:
            self.vcs.prepare_worktree(project.local_path, wt_dir, phase.branch, base_branch=base_branch)
        except Exception as e:
            self.store.park_phase(phase.id, reason=f"Worktree creation failed: {e}", next_step="Inspect repo / disk")
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
            delivered = False

            while implement_attempts < self.scheduler.max_implement_attempts:
                if not self.scheduler.has_run_budget_remaining():
                    self.store.park_phase(
                        phase.id,
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

                choice = self.scheduler.select_engine(
                    phase_id=phase.id,
                    step_kind="implement",
                    current_engine=active_runner_name if active_runner_name != "unknown" else None,
                )
                if not choice:
                    # All engines exhausted -> cooldown wait
                    earliest = self.quota.earliest_cooldown_expiry()
                    earliest_str = earliest.strftime("%H:%M") if earliest else "soon"
                    self.events.emit(Event(
                        kind="quota.exhausted",
                        plain_text=f"All engines cooling down. Waiting for earliest reset at {earliest_str}.",
                        phase_id=phase.id,
                    ))
                    self.store.park_phase(phase.id, reason=f"All engines cooling down until {earliest_str}", next_step="Resume after cooldown")
                    return {"phase_id": phase.id, "title": phase.title, "runner": "none", "status": "PARKED", "detail": f"All engines in cooldown until {earliest_str}"}

                active_runner_name = choice.engine
                runner_obj = self.runners.get(active_runner_name)
                if not runner_obj:
                    runner_obj = list(self.runners.values())[0]

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
                    phase.id, active_runner_name, phase.timeout_minutes * 60
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

                # Run diff guards
                diff_text = self.vcs.get_diff(wt_dir)
                changed_files = self.vcs.list_changed_files(wt_dir)
                findings = check_diff_guards(wt_dir, changed_files, diff_text)

                for f in findings:
                    self.store.record_guard_result(phase.id, f.guard, passed=False, detail=f.message)

                if res.rc == 0 and ok_deliv and not findings:
                    delivered = True
                    self.store.update_step(step_id, rc=0, verified=1, detail=deliv_reason)
                    self.store.record_duration("implement", active_runner_name, time.time() - t0)
                    self.events.emit(Event(
                        kind="runner.completed",
                        plain_text=f"{active_runner_name} delivered {phase.title}: {deliv_reason}",
                        phase_id=phase.id,
                        raw_path=str(raw_log_path),
                    ))
                    break

                # Failure, diff-guard finding, or quota exhaustion -> self-heal / mid-phase handoff
                failure_reason = "Diff guard failed: " + "; ".join(f.message for f in findings) if findings else deliv_reason if not ok_deliv else res.stderr[:200]
                self.store.update_step(step_id, rc=res.rc, verified=0, detail=failure_reason)
                if findings:
                    findings_summary = "\n".join(f"- [{f.guard}] {f.message}" for f in findings)
                    prompt_path.write_text(
                        guardrail_header + phase.prompt + f"\n\nDIFF GUARD FINDINGS TO FIX:\n{findings_summary}\n",
                        encoding="utf-8",
                    )

                # Look for next engine to hand off to
                next_choice = self.scheduler.select_engine(
                    phase_id=phase.id,
                    step_kind="implement",
                    current_engine=active_runner_name,
                    exclude_engines={active_runner_name},
                )
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
                self.store.park_phase(phase.id, reason="Exhausted implementer attempts without valid delivery", next_step="Manual inspection")
                return {"phase_id": phase.id, "title": phase.title, "runner": active_runner_name, "status": "PARKED", "detail": "Exhausted implementer attempts"}

            pr_info = self.vcs.find_open_pr(project.repo, phase.branch)
            if not pr_info:
                self.store.park_phase(phase.id, reason="No open PR found after implementation delivery", next_step="Open PR manually")
                return {"phase_id": phase.id, "title": phase.title, "runner": active_runner_name, "status": "PARKED", "detail": "No open PR found"}
            pr_number = int(pr_info["number"])
            pr_url = str(pr_info["url"])
            self.store.update_phase(phase.id, pr_number=pr_number)

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
            self.store.park_phase(phase.id, reason=ci_res.detail, next_step="Fix remaining CI failure")
            return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": ci_res.detail}

        detail = f"PR #{pr_number} green ({pr_url})"
        if ci_res.flaky:
            detail += " [flaky test resolved]"

        # Step 4: Merge PR if auto_merge is enabled (verify exact head SHA match)
        if phase.auto_merge:
            latest_pr = self.vcs.find_open_pr(project.repo, phase.branch)
            if (
                ci_res.checked_sha
                and latest_pr
                and latest_pr.get("headRefOid")
                and str(latest_pr["headRefOid"]) != ci_res.checked_sha
            ):
                mismatch_reason = (
                    f"Hard guard: PR #{pr_number} head SHA changed after CI passed "
                    f"(checked {ci_res.checked_sha[:7]}, now {str(latest_pr['headRefOid'])[:7]})."
                )
                self.store.record_guard_result(phase.id, "exact_head_sha", passed=False, detail=mismatch_reason)
                self.store.park_phase(phase.id, reason=mismatch_reason, next_step="Re-run CI on the latest head commit")
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
            ))
            merge_ok, merge_msg = self.vcs.merge_pr(project.repo, pr_number, project.local_path)
            if not merge_ok:
                self.store.park_phase(phase.id, reason=f"Merge failed: {merge_msg}", next_step="Resolve merge conflict or checks")
                return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": f"Merge failed: {merge_msg}"}

            self.store.update_phase(phase.id, status="merged")
            detail = f"Merged PR #{pr_number}"

            # Step 5: Post-merge release if release_after_merge is enabled
            if phase.release_after_merge:
                self.store.update_phase(phase.id, status="releasing")
                self.events.emit(Event(
                    kind="phase.releasing",
                    plain_text=f"Watching post-merge main build and publishing release for {phase.title}.",
                    phase_id=phase.id,
                ))
                try:
                    tag, published_files = self.release_mgr.publish_verified_release(
                        repo=project.repo,
                        output_dir=project.output_dir,
                        title_note=phase.title,
                        workflow_name=project.release_workflow,
                        artifact_glob=project.artifact_glob,
                        tag_prefix=project.tag_prefix,
                    )
                    detail += f" · Released {tag} ({len(published_files)} files)"
                    self.events.emit(Event(
                        kind="release.published",
                        plain_text=f"Published verified release {tag} with SHA-256 checksums.",
                        phase_id=phase.id,
                    ))
                except Exception as rel_err:
                    # Post-merge main build failure -> auto-revert (BLUEPRINT 5d)
                    logger.error("Post-merge release failed: %s", rel_err)
                    head_sha = self.vcs.remote_branch_sha(project.local_path, "main") or "HEAD"
                    self.ci_pipeline.handle_post_merge_failure(project.local_path, head_sha, phase.id, str(rel_err))
                    return {"phase_id": phase.id, "title": phase.title, "runner": ci_res.last_engine, "status": "PARKED", "detail": f"Post-merge failure: {rel_err}"}

        # Cleanup worktree
        self.vcs.remove_worktree(project.local_path, wt_dir)
        self.store.update_phase(phase.id, status="completed")

        return {
            "phase_id": phase.id,
            "title": phase.title,
            "runner": ci_res.last_engine,
            "status": "COMPLETED",
            "detail": detail,
        }
