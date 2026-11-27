"""Phase 8c tests: Durable task state machine, blocked_from_status resume,
remote_operations outbox, lease/fencing, and crash/offline resume with zero duplicates.

Covers docs/AGENT_PIPELINE.md sections 4, 5, and 12.1 Phase 8c requirements (Amendment D).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.pipeline_contracts import MergeGate
from nightshift.engine.pipeline_orchestrator import (
    ALLOWED_TRANSITIONS,
    NETWORK_BACKOFF_SECONDS,
    TASK_STATES,
    InvalidTransitionError,
    LeaseConflictError,
    NetworkTransientError,
    TaskOrchestrator,
    async_run_with_network_backoff,
    network_backoff_delay,
    run_with_network_backoff,
)
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
from nightshift.engine.vcs import FakeGitGh
from nightshift.events import EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.fake import FakeRunner
from nightshift.store import Store


def _ok_login_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    return (0, '{"loggedIn": true}\nLogged in to github.com\nOK', "", False)


def _setup_task(
    store: Store,
    *,
    task_id: str = "t-8c-01",
    branch: str = "feat/p8c-test",
    risk_class: str = "business_logic",
    owner_approval_required: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if store.get_project("proj") is None:
        store.create_project("proj", "Proj", "owner/repo", "/tmp/repo")
    if store.get_plan("plan-1") is None:
        store.create_plan("plan-1", "proj", "Plan 1")
    if store.get_phase("p1") is None:
        store.upsert_phase("p1", "plan-1", "Phase 1", branch)
    return store.create_task_with_contract(
        task_id=task_id,
        phase_id="p1",
        plan_id="plan-1",
        title="Durable orchestration task",
        branch=branch,
        risk_class=risk_class,
        owner_approval_required=owner_approval_required,
    )


def _set_task_status_directly(store: Store, task_id: str, status: str) -> None:
    with store.connection:
        store.connection.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


# ---------------------------------------------------------------------------
# 1. Exhaustive legal & illegal state transitions in atomic BEGIN IMMEDIATE tx
# ---------------------------------------------------------------------------


def test_all_legal_and_illegal_transitions_exhaustive(tmp_path: Path) -> None:
    with Store(tmp_path / "transitions.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        _setup_task(store, task_id="t-matrix", branch="feat/matrix")

        tx_counter = 0
        for from_state in sorted(TASK_STATES):
            legal_targets = ALLOWED_TRANSITIONS[from_state]
            for to_state in sorted(TASK_STATES):
                _set_task_status_directly(store, "t-matrix", from_state)
                tx_counter += 1
                key = f"tx-{tx_counter}-{from_state}-{to_state}"
                before_count = len(store.list_task_transitions("t-matrix"))

                if to_state in legal_targets:
                    updated = orch.transition(
                        "t-matrix",
                        to_state,
                        idempotency_key=key,
                        reason=f"Legal {from_state} -> {to_state}",
                    )
                    assert updated["status"] == to_state
                    assert len(store.list_task_transitions("t-matrix")) == before_count + 1
                    if to_state == "BLOCKED":
                        assert updated["blocked_from_status"] == from_state
                    if from_state == "BLOCKED":
                        assert updated["blocked_from_status"] == ""
                        assert updated["block_reason"] == ""
                    if to_state == "FAILED":
                        assert updated["failure_reason"] == f"Legal {from_state} -> {to_state}"
                        assert updated["finished_at"] is not None
                else:
                    with pytest.raises(InvalidTransitionError):
                        orch.transition(
                            "t-matrix",
                            to_state,
                            idempotency_key=key,
                            reason=f"Illegal {from_state} -> {to_state}",
                        )
                    # Atomic rollback: status and transitions unchanged
                    cur = store.get_task("t-matrix")
                    assert cur is not None and cur["status"] == from_state
                    assert len(store.list_task_transitions("t-matrix")) == before_count


def test_transition_idempotency_cross_task_collision_and_column_guard(tmp_path: Path) -> None:
    with Store(tmp_path / "idemp.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        _setup_task(store, task_id="t-a", branch="feat/a")
        _setup_task(store, task_id="t-b", branch="feat/b")

        first = orch.transition("t-a", "RUNNING", idempotency_key="start-a", reason="Start A")
        assert first["status"] == "RUNNING"
        assert len(store.list_task_transitions("t-a")) == 1

        # Replay same key on same task is idempotent (no duplicate transition, no error)
        replay = orch.transition("t-a", "RUNNING", idempotency_key="start-a", reason="Start A replay")
        assert replay["status"] == "RUNNING"
        assert len(store.list_task_transitions("t-a")) == 1

        # Reusing same idempotency key on a different task must fail
        with pytest.raises(InvalidTransitionError, match="already used"):
            orch.transition("t-b", "RUNNING", idempotency_key="start-a", reason="Collision")

        # Attempting to mutate immutable contract columns via transition updates must fail
        with pytest.raises(InvalidTransitionError, match="Disallowed"):
            orch.transition(
                "t-b",
                "RUNNING",
                idempotency_key="bad-col",
                reason="Tamper attempt",
                updates={"contract_sha256": "0" * 64},
            )


def test_failed_to_queued_explicit_retry_resets_budget_counters(tmp_path: Path) -> None:
    with Store(tmp_path / "retry.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        _setup_task(store, task_id="t-fail", branch="feat/fail")

        orch.transition("t-fail", "RUNNING", idempotency_key="k1", reason="run", updates={"attempt_count": 4, "ci_fix_rounds": 4})
        failed = orch.transition("t-fail", "FAILED", idempotency_key="k2", reason="Budget exhausted")
        assert failed["status"] == "FAILED"
        assert failed["failure_reason"] == "Budget exhausted"
        assert failed["finished_at"] is not None

        retried = orch.retry_failed_task("t-fail", idempotency_key="k3")
        assert retried["status"] == "QUEUED"
        assert retried["failure_reason"] == ""
        assert retried["finished_at"] is None
        assert retried["attempt_count"] == 0
        assert retried["ci_fix_rounds"] == 0


# ---------------------------------------------------------------------------
# 2. Lease acquisition, renewal, fencing, and expiry
# ---------------------------------------------------------------------------


def test_lease_fencing_blocks_concurrent_and_stale_workers(tmp_path: Path) -> None:
    with Store(tmp_path / "lease.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        _setup_task(store, task_id="t-lease", branch="feat/lease")

        token1 = orch.acquire_lease("t-lease", worker_pid=1111, lease_seconds=600)
        assert token1

        # Second worker cannot steal active lease
        with pytest.raises(LeaseConflictError):
            orch.acquire_lease("t-lease", worker_pid=2222, lease_seconds=600)

        # Active worker can renew and execute fenced operations
        new_exp = orch.renew_lease("t-lease", token1, lease_seconds=900)
        assert new_exp
        op = orch.begin_remote_operation(
            task_id="t-lease",
            op_kind="pr_create",
            idempotency_key="op-lease-1",
            lease_token=token1,
            target_ref="feat/lease",
        )
        assert op["status"] == "pending"

        # Stale / forged lease token cannot transition or mutate remote_operations
        with pytest.raises(LeaseConflictError):
            orch.transition("t-lease", "RUNNING", idempotency_key="stale-tx", reason="stale", lease_token="forged")
        with pytest.raises(LeaseConflictError):
            orch.begin_remote_operation(
                task_id="t-lease",
                op_kind="pr_merge",
                idempotency_key="op-stale",
                lease_token="forged",
                target_ref="42",
            )
        with pytest.raises(LeaseConflictError):
            orch.complete_remote_operation("op-lease-1", status="succeeded", lease_token="forged")

        # Expire token1 -> worker 2 takes over -> token1 is now fenced out
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        with store.connection:
            store.connection.execute("UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (past, "t-lease"))

        token2 = orch.acquire_lease("t-lease", worker_pid=2222, lease_seconds=600)
        assert token2 != token1
        with pytest.raises(LeaseConflictError):
            orch.renew_lease("t-lease", token1)
        with pytest.raises(LeaseConflictError):
            orch.begin_remote_operation(
                task_id="t-lease",
                op_kind="pr_create",
                idempotency_key="op-old-worker",
                lease_token=token1,
                target_ref="feat/lease",
            )

        assert orch.release_lease("t-lease", token1) is False
        assert orch.release_lease("t-lease", token2) is True


# ---------------------------------------------------------------------------
# 3. blocked_from_status resume without restarting coding (full revalidation)
# ---------------------------------------------------------------------------


def test_blocked_from_status_resumes_without_restarting_coding(tmp_path: Path) -> None:
    with Store(tmp_path / "resume_blocked.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        task, contract = _setup_task(
            store,
            task_id="t-gov",
            branch="feat/gov",
            risk_class="governance",
            owner_approval_required=True,
        )

        # Progress to REVIEWING and block awaiting owner approval
        orch.transition("t-gov", "RUNNING", idempotency_key="g1", reason="start")
        orch.transition("t-gov", "VERIFYING", idempotency_key="g2", reason="pr open", updates={"head_sha": "sha_111", "pr_number": 12})
        store.record_evidence(
            task_id="t-gov",
            contract_sha256=contract["contract_sha256"],
            commit_sha="sha_111",
            kind="ci_check",
            source="circleci:desktop-backend",
            duration_seconds=32.0,
            verified=True,
        )
        orch.transition("t-gov", "REVIEWING", idempotency_key="g3", reason="ci green", updates={"review_sha": "sha_111"})
        blocked = orch.transition("t-gov", "BLOCKED", idempotency_key="g4", reason="awaiting_owner_approval")
        assert blocked["blocked_from_status"] == "REVIEWING"

        # 1. Head SHA drift must block resume
        with pytest.raises(InvalidTransitionError, match="head_sha drifted"):
            orch.resume_from_blocked("t-gov", current_head_sha="sha_DRIFT", owner_approved_by="owner")

        # 2. Owner approval on exact head_sha resumes directly to APPROVED (never QUEUED!)
        resumed = orch.resume_from_blocked(
            "t-gov",
            current_head_sha="sha_111",
            owner_approved_by="owner",
            idempotency_key="g5-approve",
        )
        assert resumed["status"] == "APPROVED"
        assert resumed["approved_sha"] == "sha_111"
        assert resumed["owner_approved_sha"] == "sha_111"
        assert resumed["blocked_from_status"] == ""

        # 3. Block from MERGED (e.g. partial release / smoke unavailable) -> resumes to MERGED, not QUEUED
        orch.transition("t-gov", "MERGED", idempotency_key="g6", reason="merged", updates={"merge_sha": "merge_999"})
        blocked_merged = orch.transition("t-gov", "BLOCKED", idempotency_key="g7", reason="smoke_test_unavailable")
        assert blocked_merged["blocked_from_status"] == "MERGED"

        with pytest.raises(InvalidTransitionError, match="merge_sha drifted"):
            orch.resume_from_blocked("t-gov", current_merge_sha="merge_OTHER")

        resumed_merged = orch.resume_from_blocked("t-gov", current_merge_sha="merge_999", idempotency_key="g8")
        assert resumed_merged["status"] == "MERGED"

        # 4. Contract revision invalidates evidence -> resume to APPROVED must fail until re-verified
        _setup_task(store, task_id="t-rev", branch="feat/rev")
        orch.transition("t-rev", "RUNNING", idempotency_key="r1", reason="start")
        orch.transition("t-rev", "VERIFYING", idempotency_key="r2", reason="pr", updates={"head_sha": "sha_222"})
        orch.transition("t-rev", "REVIEWING", idempotency_key="r3", reason="ci", updates={"review_sha": "sha_222"})
        orch.transition("t-rev", "APPROVED", idempotency_key="r4", reason="ok", updates={"approved_sha": "sha_222"})
        orch.transition("t-rev", "BLOCKED", idempotency_key="r5", reason="contract_revision_needed")
        store.revise_task_contract(task_id="t-rev", allowed_globs=["backend/nightshift/**"])

        with pytest.raises(InvalidTransitionError, match="verified CI evidence missing"):
            orch.resume_from_blocked("t-rev", current_head_sha="sha_222")

        # Explicit owner restart returns to QUEUED
        restarted = orch.resume_from_blocked("t-rev", restart_to_queued=True, idempotency_key="r6")
        assert restarted["status"] == "QUEUED"


# ---------------------------------------------------------------------------
# 4. remote_operations outbox: PR, merge, and partial release reconciliation
# ---------------------------------------------------------------------------


def test_remote_operations_pr_create_reconciliation(tmp_path: Path) -> None:
    with Store(tmp_path / "pr_outbox.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)

        # Open PR adopted
        _setup_task(store, task_id="t-open", branch="feat/open")
        tok = orch.acquire_lease("t-open")
        res = orch.reconcile_pr_create(
            task_id="t-open",
            branch="feat/open",
            lease_token=tok,
            existing_prs=[{"number": 10, "state": "OPEN", "url": "https://gh/10", "headRefOid": "sha10"}],
        )
        assert res.action == "adopted_open_pr"
        assert res.data["number"] == 10
        op = store.get_remote_operation("pr_create:t-open:feat/open")
        assert op is not None and op["status"] == "succeeded"

        # Merged PR adopted
        _setup_task(store, task_id="t-merged", branch="feat/merged")
        tok_m = orch.acquire_lease("t-merged")
        res_m = orch.reconcile_pr_create(
            task_id="t-merged",
            branch="feat/merged",
            lease_token=tok_m,
            existing_prs=[{"number": 11, "state": "MERGED", "mergeCommit": {"oid": "m11"}}],
        )
        assert res_m.action == "adopted_merged_pr"

        # Closed unmerged PR blocks (refuses to silently duplicate)
        _setup_task(store, task_id="t-closed", branch="feat/closed")
        tok_c = orch.acquire_lease("t-closed")
        res_c = orch.reconcile_pr_create(
            task_id="t-closed",
            branch="feat/closed",
            lease_token=tok_c,
            existing_prs=[{"number": 12, "state": "CLOSED"}],
        )
        assert res_c.action == "blocked"
        assert res_c.data["block_reason"] == "closed_pr_exists"
        op_c = store.get_remote_operation("pr_create:t-closed:feat/closed")
        assert op_c is not None and op_c["status"] == "failed"

        # API error fails closed as ambiguous
        _setup_task(store, task_id="t-amb", branch="feat/amb")
        tok_a = orch.acquire_lease("t-amb")
        res_a = orch.reconcile_pr_create(
            task_id="t-amb",
            branch="feat/amb",
            lease_token=tok_a,
            pr_list_lookup=lambda: (_ for _ in ()).throw(RuntimeError("502 Bad Gateway")),
        )
        assert res_a.action == "blocked"
        assert res_a.data["block_reason"] == "ambiguous_remote_operation"
        op_a = store.get_remote_operation("pr_create:t-amb:feat/amb")
        assert op_a is not None and op_a["status"] == "ambiguous"


def test_remote_operations_merge_reconciliation_and_race_safe_base(tmp_path: Path) -> None:
    with Store(tmp_path / "merge_outbox.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        _setup_task(store, task_id="t-merge", branch="feat/merge")
        tok = orch.acquire_lease("t-merge")

        # 1. Base branch moved -> base_moved (caller updates branch & re-verifies)
        r_base = orch.reconcile_merge_outcome(
            task_id="t-merge",
            pr_number=50,
            approved_sha="sha_ok",
            lease_token=tok,
            pr_state_lookup=lambda: {"number": 50, "state": "OPEN", "headRefOid": "sha_ok"},
            expected_base_sha="base_old",
            current_base_sha="base_new",
        )
        assert r_base.action == "base_moved"

        # 2. Ambiguous API error -> fail closed to BLOCKED
        r_err = orch.reconcile_merge_outcome(
            task_id="t-merge",
            pr_number=51,
            approved_sha="sha_ok",
            lease_token=tok,
            pr_state_lookup=lambda: (_ for _ in ()).throw(TimeoutError("API timeout")),
        )
        assert r_err.action == "blocked"
        assert r_err.data["block_reason"] == "ambiguous_remote_operation"
        op = store.get_remote_operation("pr_merge:t-merge:51:sha_ok")
        assert op is not None and op["status"] == "ambiguous"

        # 3. Already merged -> adopted_merged_pr (zero duplicate merge)
        r_done = orch.reconcile_merge_outcome(
            task_id="t-merge",
            pr_number=52,
            approved_sha="sha_ok",
            lease_token=tok,
            pr_state_lookup=lambda: {"number": 52, "state": "MERGED", "mergeCommit": {"oid": "merge_52"}},
        )
        assert r_done.action == "adopted_merged_pr"
        assert r_done.data["merge_sha"] == "merge_52"
        op_done = store.get_remote_operation("pr_merge:t-merge:52:sha_ok")
        assert op_done is not None and op_done["status"] == "succeeded"


def test_remote_operations_release_reconciliation_and_partial_release(tmp_path: Path) -> None:
    with Store(tmp_path / "rel_outbox.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        _setup_task(store, task_id="t-rel", branch="feat/rel")
        tok = orch.acquire_lease("t-rel")

        # 1. No existing release -> proceed (tracks both release_create & release_asset_upload)
        r_new = orch.reconcile_release_for_merge_sha(
            task_id="t-rel",
            merge_sha="merge_aaa",
            expected_asset_names=["NightShift-windows.zip"],
            lease_token=tok,
            existing_releases=[],
        )
        assert r_new.action == "proceed"
        assert store.get_remote_operation("release_create:t-rel:merge_aaa") is not None
        assert store.get_remote_operation("release_asset_upload:t-rel:merge_aaa") is not None

        # 2. Complete existing release -> adopt without creating duplicate
        r_adopt = orch.reconcile_release_for_merge_sha(
            task_id="t-rel",
            merge_sha="merge_bbb",
            expected_asset_names=["NightShift-windows.zip"],
            lease_token=tok,
            existing_releases=[
                {
                    "tag": "v1.0.0-preview.7",
                    "body": "Notes\n<!-- nightshift-merge-sha: merge_bbb -->",
                    "assets": [{"name": "NightShift-windows.zip"}, {"name": "SHA256SUMS.txt"}],
                    "checksums": {"NightShift-windows.zip": "abc123"},
                }
            ],
            expected_checksums={"NightShift-windows.zip": "abc123"},
        )
        assert r_adopt.action == "adopted_release"
        assert r_adopt.data["tag"] == "v1.0.0-preview.7"
        assert store.get_remote_operation("release_create:t-rel:merge_bbb")["status"] == "succeeded"
        assert store.get_remote_operation("release_asset_upload:t-rel:merge_bbb")["status"] == "succeeded"

        # 3. Partial release (missing SHA256SUMS.txt) -> fail closed, never overwrite tag/assets
        r_partial = orch.reconcile_release_for_merge_sha(
            task_id="t-rel",
            merge_sha="merge_ccc",
            expected_asset_names=["NightShift-windows.zip"],
            lease_token=tok,
            existing_releases=[
                {
                    "tag": "v1.0.0-preview.8",
                    "body": "Notes\n<!-- nightshift-merge-sha: merge_ccc -->",
                    "assets": [{"name": "NightShift-windows.zip"}],
                }
            ],
        )
        assert r_partial.action == "blocked"
        assert r_partial.data["block_reason"] == "partial_release_repair"
        assert r_partial.data["missing_assets"] == ["SHA256SUMS.txt"]
        assert store.get_remote_operation("release_asset_upload:t-rel:merge_ccc")["status"] == "failed"


# ---------------------------------------------------------------------------
# 5. Orphaned RUNNING task crash recovery & network-loss backoff
# ---------------------------------------------------------------------------


def test_recover_orphaned_running_tasks_checkpoints_dead_and_expired_only(tmp_path: Path) -> None:
    with Store(tmp_path / "orphan.db") as store:
        store.migrate()
        orch = TaskOrchestrator(store)
        _setup_task(store, task_id="t-dead", branch="feat/dead")
        _setup_task(store, task_id="t-expired", branch="feat/expired")
        _setup_task(store, task_id="t-alive", branch="feat/alive")

        orch.acquire_lease("t-dead", worker_pid=99991, lease_seconds=600)
        orch.transition("t-dead", "RUNNING", idempotency_key="d1", reason="start")

        orch.acquire_lease("t-expired", worker_pid=99992, lease_seconds=600)
        orch.transition("t-expired", "RUNNING", idempotency_key="e1", reason="start")
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        with store.connection:
            store.connection.execute("UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (past, "t-expired"))

        alive_tok = orch.acquire_lease("t-alive", worker_pid=99993, lease_seconds=600)
        orch.transition("t-alive", "RUNNING", idempotency_key="a1", reason="start")

        checkpointed: list[str] = []
        recovered = orch.recover_orphaned_running_tasks(
            is_pid_alive=lambda pid: pid in (99992, 99993),
            checkpoint_fn=lambda t: checkpointed.append(str(t["id"])),
        )
        assert set(recovered) == {"t-dead", "t-expired"}
        assert set(checkpointed) == {"t-dead", "t-expired"}

        assert store.get_task("t-dead")["lease_token"] is None
        assert store.get_task("t-expired")["lease_token"] is None
        assert store.get_task("t-alive")["lease_token"] == alive_tok


@pytest.mark.asyncio
async def test_network_backoff_schedule_never_increments_attempt_count(tmp_path: Path) -> None:
    assert [network_backoff_delay(i) for i in range(1, 7)] == [30, 60, 120, 300, 300, 300]
    assert NETWORK_BACKOFF_SECONDS == (30, 60, 120, 300)

    with Store(tmp_path / "net.db") as store:
        store.migrate()
        _setup_task(store, task_id="t-net", branch="feat/net")

        sleeps: list[float] = []
        calls = 0

        def flaky_sync() -> str:
            nonlocal calls
            calls += 1
            if calls < 4:
                raise NetworkTransientError("Could not resolve host: api.github.com")
            return "ok"

        result = run_with_network_backoff(flaky_sync, max_retries=4, sleep_fn=sleeps.append)
        assert result == "ok"
        assert sleeps == [30, 60, 120]
        assert store.get_task("t-net")["attempt_count"] == 0

        # Async variant also preserves backoff and zero attempt burn
        async_sleeps: list[float] = []
        acalls = 0

        async def flaky_async() -> str:
            nonlocal acalls
            acalls += 1
            if acalls < 3:
                raise ConnectionError("Connection reset by peer")
            return "async-ok"

        async def fake_sleep(sec: float) -> None:
            async_sleeps.append(sec)

        ares = await async_run_with_network_backoff(flaky_async, max_retries=4, sleep_fn=fake_sleep)
        assert ares == "async-ok"
        assert async_sleeps == [30, 60]
        assert store.get_task("t-net")["attempt_count"] == 0

        # Non-network errors raise immediately without backoff
        with pytest.raises(ValueError, match="fatal logic error"):
            run_with_network_backoff(lambda: (_ for _ in ()).throw(ValueError("fatal logic error")), sleep_fn=sleeps.append)


# ---------------------------------------------------------------------------
# 6. E2E crash-resume simulations (mid-RUNNING, VERIFYING, APPROVED, MERGED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_crash_mid_running_and_mid_verifying_zero_duplicate_prs(tmp_path: Path) -> None:
    """Crash after PR created (mid-RUNNING / mid-VERIFYING) -> restart adopts PR with zero duplicate PRs."""
    with Store(tmp_path / "crash_run.db") as store:
        store.migrate()
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        # Simulate crash: previous worker died in RUNNING state after opening PR #77
        store.create_project("p", "P", "o/r", str(tmp_path))
        store.create_plan("plan_default", "p", "Plan")
        store.upsert_phase("p1", "plan_default", "P1", "feat/p1", status="implementing")
        task, _ = store.create_task_with_contract(
            task_id="task-p1",
            phase_id="p1",
            plan_id="plan_default",
            title="P1",
            branch="feat/p1",
        )
        orch = TaskOrchestrator(store)
        orch.acquire_lease(task["id"], worker_pid=999999, lease_seconds=600)
        orch.transition(task["id"], "RUNNING", idempotency_key="prev-run", reason="previous worker")
        vcs.add_pr("feat/p1", number=77, head_sha="sha_77")

        runner = FakeRunner(name="agy", on_call=lambda *a: vcs.add_pr("feat/p1", number=999))
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(
            store,
            EventBus(store),
            quota,
            vcs=vcs,
            runners={"agy": runner},
            login_checker=logins,
            merge_gate=MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=False),
        )
        project = ProjectConfig(id="p", name="P", repo="o/r", local_path=tmp_path, output_dir=tmp_path / "out")

        with (
            patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"),
            patch("nightshift.engine.state_machine.is_process_alive", return_value=False),
        ):
            rows = await engine.execute_plan(
                project,
                [PhaseDefinition(id="p1", title="P1", branch="feat/p1", release_after_merge=False)],
            )

        assert rows[0]["status"] == "COMPLETED"
        # Coder was NEVER re-invoked and no duplicate PR was opened
        assert len(runner.calls) == 0
        assert vcs.merged_prs["feat/p1"]["number"] == 77
        assert store.get_task("task-p1")["status"] == "MERGED"
        # Lease was cleanly released on exit
        assert store.get_task("task-p1")["lease_token"] is None


@pytest.mark.asyncio
async def test_e2e_crash_mid_merged_zero_duplicate_releases_and_partial_release_blocks(tmp_path: Path) -> None:
    """Crash mid-MERGED: complete release is adopted (zero duplicates); partial release fails closed."""
    # Case A: Complete release already exists for merge_sha -> adopted, zero duplicate release created
    with Store(tmp_path / "crash_rel_ok.db") as store:
        store.migrate()
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        vcs.merged_prs["feat/p1"] = {"number": 88, "state": "MERGED", "mergeCommit": {"oid": "merge_88"}}
        vcs.releases.append(
            {
                "tag": "v1.0.0-preview.5",
                "title": "repo v1.0.0-preview.5",
                "body": "Auto-build\n<!-- nightshift-merge-sha: merge_88 -->",
                "assets": [{"name": "NightShift-test.zip"}, {"name": "SHA256SUMS.txt"}],
            }
        )
        store.create_project("p", "P", "o/r", str(tmp_path))
        store.create_plan("plan_default", "p", "Plan")
        store.upsert_phase("p1", "plan_default", "P1", "feat/p1", status="releasing", pr_number=88)
        store.create_task_with_contract(
            task_id="task-p1",
            phase_id="p1",
            plan_id="plan_default",
            title="P1",
            branch="feat/p1",
        )
        store.update_task_fields("task-p1", status="MERGED", merge_sha="merge_88")

        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, EventBus(store), quota, vcs=vcs, runners={}, login_checker=logins)
        project = ProjectConfig(
            id="p",
            name="P",
            repo="o/r",
            local_path=tmp_path,
            output_dir=tmp_path / "out",
            release_workflow="Desktop build",
            artifact_glob="*.zip",
        )
        rows = await engine.execute_plan(
            project,
            [PhaseDefinition(id="p1", title="P1", branch="feat/p1", release_after_merge=True)],
        )
        assert rows[0]["status"] == "COMPLETED"
        assert "Adopted release v1.0.0-preview.5" in rows[0]["detail"]
        # Still exactly 1 release — zero duplicates
        assert len(vcs.releases) == 1
        assert store.get_task("task-p1")["release_tag"] == "v1.0.0-preview.5"

    # Case B: Partial release (missing SHA256SUMS.txt) -> fail closed to BLOCKED, never overwrite tag
    with Store(tmp_path / "crash_rel_partial.db") as store:
        store.migrate()
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        vcs.merged_prs["feat/p2"] = {"number": 89, "state": "MERGED", "mergeCommit": {"oid": "merge_89"}}
        vcs.releases.append(
            {
                "tag": "v1.0.0-preview.6",
                "title": "repo v1.0.0-preview.6",
                "body": "Auto-build\n<!-- nightshift-merge-sha: merge_89 -->",
                "assets": [{"name": "NightShift-test.zip"}],  # missing SHA256SUMS.txt!
            }
        )
        store.create_project("p", "P", "o/r", str(tmp_path))
        store.create_plan("plan_default", "p", "Plan")
        store.upsert_phase("p2", "plan_default", "P2", "feat/p2", status="releasing", pr_number=89)
        store.create_task_with_contract(
            task_id="task-p2",
            phase_id="p2",
            plan_id="plan_default",
            title="P2",
            branch="feat/p2",
        )
        store.update_task_fields("task-p2", status="MERGED", merge_sha="merge_89")

        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, EventBus(store), quota, vcs=vcs, runners={}, login_checker=logins)
        project = ProjectConfig(
            id="p",
            name="P",
            repo="o/r",
            local_path=tmp_path,
            output_dir=tmp_path / "out",
            release_workflow="Desktop build",
            artifact_glob="*.zip",
        )
        rows = await engine.execute_plan(
            project,
            [PhaseDefinition(id="p2", title="P2", branch="feat/p2", release_after_merge=True)],
        )
        assert rows[0]["status"] == "PARKED"
        assert "Partial release" in rows[0]["detail"]
        assert len(vcs.releases) == 1
        t2 = store.get_task("task-p2")
        assert t2 is not None and t2["status"] == "BLOCKED"
        assert t2["blocked_from_status"] == "MERGED"
        assert t2["block_reason"] == "partial_release_repair"


@pytest.mark.asyncio
async def test_e2e_closed_unmerged_pr_blocks_without_duplicating_pr(tmp_path: Path) -> None:
    """If a closed (unmerged) PR exists for the branch, fail closed to BLOCKED instead of creating a duplicate."""
    with Store(tmp_path / "closed_pr.db") as store:
        store.migrate()
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        vcs.add_closed_pr("feat/closed-branch", number=301)

        runner = FakeRunner(name="agy", on_call=lambda *a: vcs.add_pr("feat/closed-branch", number=302))
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(
            store,
            EventBus(store),
            quota,
            vcs=vcs,
            runners={"agy": runner},
            login_checker=logins,
        )
        project = ProjectConfig(id="p", name="P", repo="o/r", local_path=tmp_path, output_dir=tmp_path / "out")

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            rows = await engine.execute_plan(
                project,
                [PhaseDefinition(id="p-closed", title="Closed PR phase", branch="feat/closed-branch")],
            )

        assert rows[0]["status"] == "PARKED"
        assert len(runner.calls) == 0
        task = store.get_task("task-p-closed")
        assert task is not None and task["status"] == "BLOCKED"
        assert task["block_reason"] == "closed_pr_exists"


@pytest.mark.asyncio
async def test_e2e_crash_mid_approved_merge_timeout_reconciles_without_duplicate_merge(tmp_path: Path) -> None:
    """Crash/timeout during merge_pr after GitHub actually merged -> reconciles and adopts merge_sha with zero duplicate merges."""
    class CrashAfterMergeVcs(FakeGitGh):
        def __init__(self) -> None:
            super().__init__()
            self.merge_calls = 0

        def merge_pr(
            self,
            repo: str,
            pr_number: int,
            local_repo: Path,
            strategy: str = "--merge",
            match_head_commit: str | None = None,
        ) -> tuple[bool, str]:
            self.merge_calls += 1
            # Simulate GitHub merging the PR, then network dropping before response returns
            super().merge_pr(repo, pr_number, local_repo, strategy=strategy, match_head_commit=match_head_commit)
            raise TimeoutError("Connection timed out waiting for GitHub merge response")

    with Store(tmp_path / "crash_approved.db") as store:
        store.migrate()
        quota = QuotaLedger(store)
        vcs = CrashAfterMergeVcs()
        vcs.add_pr("feat/p-merge", number=401, head_sha="sha_401")

        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(
            store,
            EventBus(store),
            quota,
            vcs=vcs,
            runners={},
            login_checker=logins,
            merge_gate=MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=False),
        )
        project = ProjectConfig(id="p", name="P", repo="o/r", local_path=tmp_path, output_dir=tmp_path / "out")

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            rows = await engine.execute_plan(
                project,
                [PhaseDefinition(id="p-merge", title="Merge timeout phase", branch="feat/p-merge", release_after_merge=False)],
            )

        assert rows[0]["status"] == "COMPLETED"
        assert vcs.merge_calls == 1
        task = store.get_task("task-p-merge")
        assert task is not None and task["status"] == "MERGED"
        assert task["merge_sha"] == "sha_merge_123"
        op = store.get_remote_operation("pr_merge:task-p-merge:401:sha_401")
        assert op is not None and op["status"] == "succeeded"

