"""Durable task state machine, remote_operations outbox, lease fencing, and crash/offline recovery.

Implements Governance Amendment D and docs/AGENT_PIPELINE.md section 4.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from nightshift.engine.pipeline_contracts import verify_contract_integrity
from nightshift.store import Store

TASK_STATES: frozenset[str] = frozenset(
    {
        "QUEUED",
        "RUNNING",
        "VERIFYING",
        "REVIEWING",
        "APPROVED",
        "MERGED",
        "RELEASED",
        "BLOCKED",
        "FAILED",
    }
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "QUEUED": frozenset({"RUNNING", "BLOCKED", "FAILED"}),
    "RUNNING": frozenset({"VERIFYING", "BLOCKED", "FAILED"}),
    "VERIFYING": frozenset({"REVIEWING", "RUNNING", "BLOCKED", "FAILED"}),
    "REVIEWING": frozenset({"APPROVED", "RUNNING", "BLOCKED", "FAILED"}),
    "APPROVED": frozenset({"MERGED", "VERIFYING", "BLOCKED"}),
    "MERGED": frozenset({"RELEASED", "BLOCKED"}),
    "BLOCKED": frozenset({"VERIFYING", "REVIEWING", "APPROVED", "MERGED", "QUEUED"}),
    "FAILED": frozenset({"QUEUED"}),
    "RELEASED": frozenset(),
}

NETWORK_BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120, 300)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def network_backoff_delay(consecutive_errors: int) -> int:
    """Return exponential network-loss backoff delay (30s -> 60s -> 120s -> 300s cap)."""
    idx = max(0, min(consecutive_errors - 1, len(NETWORK_BACKOFF_SECONDS) - 1))
    return NETWORK_BACKOFF_SECONDS[idx]


class InvalidTransitionError(ValueError):
    """Raised when an illegal task state transition is attempted."""


class LeaseConflictError(RuntimeError):
    """Raised when another active worker holds the task lease."""


@dataclass(frozen=True)
class ReconcileResult:
    action: str  # "proceed" | "adopted_open_pr" | "adopted_merged_pr" | "adopted_release" | "blocked"
    detail: str
    data: dict[str, Any]


class TaskOrchestrator:
    """Crash-safe, idempotent task state machine backed by SQLite BEGIN IMMEDIATE transactions."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def transition(
        self,
        task_id: str,
        to_status: str,
        *,
        idempotency_key: str,
        reason: str,
        actor: str = "engine",
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically transition ``task_id`` to ``to_status`` and record ``task_transitions``.

        Idempotent on ``idempotency_key``: replaying an already-applied key returns the
        current task without duplicating transitions or side effects.
        """
        if to_status not in TASK_STATES:
            raise InvalidTransitionError(f"Unknown target state: {to_status}")

        conn = self.store.connection
        now = _utc_iso()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_tx = conn.execute(
                "SELECT * FROM task_transitions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_tx is not None:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                conn.commit()
                if row is None:
                    raise KeyError(f"Unknown task: {task_id}")
                return dict(row)

            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            from_status = str(row["status"])
            if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
                raise InvalidTransitionError(f"Illegal transition {from_status} -> {to_status} for task {task_id}")

            extra: dict[str, Any] = dict(updates or {})
            extra["status"] = to_status
            extra["updated_at"] = now

            if to_status == "BLOCKED":
                extra.setdefault("blocked_from_status", from_status)
                extra.setdefault("block_reason", reason)
            elif from_status == "BLOCKED":
                extra["blocked_from_status"] = ""
                extra["block_reason"] = ""
                extra["block_detail"] = ""
                extra["next_step"] = ""

            if to_status in ("FAILED", "RELEASED"):
                extra.setdefault("finished_at", now)
            if to_status == "FAILED":
                extra.setdefault("failure_reason", reason)

            set_clause = ", ".join(f"{k} = ?" for k in extra)
            conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?",
                list(extra.values()) + [task_id],
            )
            conn.execute(
                """
                INSERT INTO task_transitions (
                    task_id, from_status, to_status, idempotency_key, reason, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, from_status, to_status, idempotency_key, reason, actor, now),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.commit()
            assert updated is not None
            return dict(updated)
        except Exception:
            conn.rollback()
            raise

    def acquire_lease(
        self,
        task_id: str,
        *,
        worker_pid: int | None = None,
        lease_seconds: int = 1800,
    ) -> str:
        """Atomically acquire a fenced lease token for ``task_id``."""
        conn = self.store.connection
        now = _utc_now()
        now_iso = _utc_iso(now)
        expires_iso = _utc_iso(now + timedelta(seconds=lease_seconds))
        token = str(uuid.uuid4())
        pid = worker_pid if worker_pid is not None else os.getpid()

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT lease_token, lease_expires_at, worker_pid FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            existing_token = row["lease_token"]
            existing_exp = row["lease_expires_at"]
            if existing_token and existing_exp:
                try:
                    exp_dt = datetime.fromisoformat(str(existing_exp))
                    if exp_dt > now:
                        raise LeaseConflictError(f"Task {task_id} already leased until {existing_exp}")
                except ValueError:
                    pass
            conn.execute(
                """
                UPDATE tasks
                SET lease_token = ?, lease_expires_at = ?, worker_pid = ?, updated_at = ?
                WHERE id = ?
                """,
                (token, expires_iso, pid, now_iso, task_id),
            )
            conn.commit()
            return token
        except Exception:
            conn.rollback()
            raise

    def release_lease(self, task_id: str, lease_token: str) -> None:
        conn = self.store.connection
        with conn:
            conn.execute(
                """
                UPDATE tasks
                SET lease_token = NULL, lease_expires_at = NULL, worker_pid = NULL, updated_at = ?
                WHERE id = ? AND lease_token = ?
                """,
                (_utc_iso(), task_id, lease_token),
            )

    def record_contributing_provider(self, task_id: str, provider: str) -> list[str]:
        """Persist ``provider`` in ``tasks.contributing_providers_json`` (idempotent set)."""
        conn = self.store.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT contributing_providers_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            providers = list(json.loads(str(row["contributing_providers_json"] or "[]")))
            if provider and provider not in providers:
                providers.append(provider)
            conn.execute(
                "UPDATE tasks SET contributing_providers_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(providers), _utc_iso(), task_id),
            )
            conn.commit()
            return providers
        except Exception:
            conn.rollback()
            raise

    def resume_from_blocked(
        self,
        task_id: str,
        *,
        current_head_sha: str | None = None,
        owner_approved_by: str | None = None,
        actor: str = "owner",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resume a BLOCKED task to its ``blocked_from_status`` (or APPROVED) after full revalidation.

        Never restarts coding (QUEUED) unless blocked_from_status was QUEUED or RUNNING.
        """
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        if task["status"] != "BLOCKED":
            raise InvalidTransitionError(f"Task {task_id} is {task['status']}, not BLOCKED")

        contract = self.store.get_task_contract(str(task["contract_id"]))
        if contract is None:
            raise InvalidTransitionError(f"Missing frozen contract {task['contract_id']} for task {task_id}")

        ok_contract, contract_msg = verify_contract_integrity(contract, expected_sha256=str(task["contract_sha256"]))
        if not ok_contract:
            raise InvalidTransitionError(f"Cannot resume task {task_id}: {contract_msg}")

        blocked_from = str(task.get("blocked_from_status") or "QUEUED")
        expected_sha = str(task.get("head_sha") or "")

        # Verify head_sha has not drifted if we are resuming post-coding
        if blocked_from in ("VERIFYING", "REVIEWING", "APPROVED") and current_head_sha and expected_sha:
            if current_head_sha != expected_sha:
                raise InvalidTransitionError(
                    f"Cannot resume task {task_id}: head_sha drifted ({expected_sha[:7]} -> {current_head_sha[:7]})."
                )

        updates: dict[str, Any] = {}
        if owner_approved_by and expected_sha:
            updates["owner_approved_by"] = owner_approved_by
            updates["owner_approved_sha"] = expected_sha
            updates["owner_approved_at"] = _utc_iso()

        # Check owner approval if required and resuming to APPROVED
        risk_class = str(task.get("risk_class") or "")
        owner_required = risk_class in ("governance", "high_risk") or bool(contract.get("owner_approval_required"))
        effective_approved_sha = str(updates.get("owner_approved_sha") or task.get("owner_approved_sha") or "")

        if blocked_from in ("REVIEWING", "APPROVED") and (owner_approved_by or not owner_required):
            # Revalidate evidence for exact head_sha before advancing to APPROVED
            evidence = self.store.list_evidence_records(task_id, commit_sha=expected_sha or None)
            has_valid_ci = any(
                e.get("kind") == "ci_check"
                and e.get("contract_sha256") == task["contract_sha256"]
                and bool(e.get("verified"))
                for e in evidence
            )
            if has_valid_ci and (not owner_required or effective_approved_sha == expected_sha):
                target = "APPROVED"
                updates["approved_sha"] = expected_sha
            elif blocked_from == "REVIEWING":
                target = "REVIEWING"
            else:
                raise InvalidTransitionError(
                    f"Cannot resume task {task_id} to APPROVED: verified CI evidence or owner approval missing for {expected_sha[:7]}."
                )
        elif blocked_from == "VERIFYING":
            target = "VERIFYING"
        elif blocked_from == "MERGED":
            target = "MERGED"
        else:
            target = "QUEUED"

        key = idempotency_key or f"resume:{task_id}:{blocked_from}:{target}:{_utc_iso()}"
        return self.transition(
            task_id,
            target,
            idempotency_key=key,
            reason=f"Resumed from BLOCKED ({blocked_from} -> {target}) after revalidation",
            actor=actor,
            updates=updates,
        )

    # ------------------------------------------------------------------
    # Durable remote_operations outbox & side-effect reconciliation
    # ------------------------------------------------------------------

    def begin_remote_operation(
        self,
        *,
        task_id: str,
        op_kind: str,
        idempotency_key: str,
        lease_token: str,
        target_ref: str,
    ) -> dict[str, Any]:
        """Write-ahead intent into ``remote_operations`` fenced by ``lease_token``."""
        conn = self.store.connection
        now = _utc_iso()
        conn.execute("BEGIN IMMEDIATE")
        try:
            task = conn.execute("SELECT lease_token FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Unknown task: {task_id}")
            if task["lease_token"] and str(task["lease_token"]) != lease_token:
                raise LeaseConflictError(f"Stale lease_token for task {task_id}; active lease belongs to another worker.")

            existing = conn.execute(
                "SELECT * FROM remote_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return dict(existing)

            conn.execute(
                """
                INSERT INTO remote_operations (
                    task_id, op_kind, idempotency_key, lease_token, target_ref,
                    status, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', '{}', ?, ?)
                """,
                (task_id, op_kind, idempotency_key, lease_token, target_ref, now, now),
            )
            created = conn.execute(
                "SELECT * FROM remote_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            conn.commit()
            assert created is not None
            return dict(created)
        except Exception:
            conn.rollback()
            raise

    def complete_remote_operation(
        self,
        idempotency_key: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn = self.store.connection
        now = _utc_iso()
        with conn:
            conn.execute(
                """
                UPDATE remote_operations
                SET status = ?, result_json = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (status, json.dumps(result or {}, sort_keys=True), now, idempotency_key),
            )
        row = conn.execute(
            "SELECT * FROM remote_operations WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        assert row is not None
        return dict(row)

    def reconcile_pr_create(
        self,
        *,
        task_id: str,
        branch: str,
        lease_token: str,
        existing_prs: list[dict[str, Any]],
    ) -> ReconcileResult:
        """Reconcile PR state before creating a PR so crashes never create duplicates."""
        op_key = f"pr_create:{task_id}:{branch}"
        self.begin_remote_operation(
            task_id=task_id,
            op_kind="pr_create",
            idempotency_key=op_key,
            lease_token=lease_token,
            target_ref=branch,
        )
        open_prs = [p for p in existing_prs if str(p.get("state", "")).upper() == "OPEN"]
        merged_prs = [p for p in existing_prs if str(p.get("state", "")).upper() == "MERGED"]
        closed_prs = [p for p in existing_prs if str(p.get("state", "")).upper() == "CLOSED"]

        if open_prs:
            pr = open_prs[0]
            self.complete_remote_operation(op_key, status="succeeded", result=pr)
            return ReconcileResult("adopted_open_pr", f"Adopted existing open PR #{pr.get('number')}", pr)
        if merged_prs:
            pr = merged_prs[0]
            self.complete_remote_operation(op_key, status="succeeded", result=pr)
            return ReconcileResult("adopted_merged_pr", f"Adopted already-merged PR #{pr.get('number')}", pr)
        if closed_prs:
            pr = closed_prs[0]
            self.complete_remote_operation(op_key, status="failed", result=pr)
            return ReconcileResult(
                "blocked",
                f"Closed unmerged PR #{pr.get('number')} exists for branch {branch}; refusing to duplicate.",
                {"block_reason": "closed_pr_exists", "pr": pr},
            )
        return ReconcileResult("proceed", f"No existing PR for {branch}; safe to create.", {"op_key": op_key})

    def reconcile_merge_outcome(
        self,
        *,
        task_id: str,
        pr_number: int,
        approved_sha: str,
        lease_token: str,
        pr_state_lookup: Callable[[], dict[str, Any] | None],
    ) -> ReconcileResult:
        """Reconcile an ambiguous merge result fail-closed (AGENT_PIPELINE §4)."""
        op_key = f"pr_merge:{task_id}:{pr_number}:{approved_sha}"
        self.begin_remote_operation(
            task_id=task_id,
            op_kind="pr_merge",
            idempotency_key=op_key,
            lease_token=lease_token,
            target_ref=str(pr_number),
        )
        try:
            pr = pr_state_lookup()
        except Exception as exc:
            self.complete_remote_operation(op_key, status="ambiguous", result={"error": str(exc)})
            return ReconcileResult(
                "blocked",
                f"Merge status ambiguous due to API error: {exc}",
                {"block_reason": "ambiguous_remote_operation"},
            )
        if pr is None:
            self.complete_remote_operation(op_key, status="ambiguous", result={"error": "PR not found"})
            return ReconcileResult(
                "blocked",
                f"Merge status ambiguous: PR #{pr_number} not found.",
                {"block_reason": "ambiguous_remote_operation"},
            )
        state = str(pr.get("state", "")).upper()
        if state == "MERGED":
            merge_sha = str((pr.get("mergeCommit") or {}).get("oid") or pr.get("merge_sha") or "")
            self.complete_remote_operation(op_key, status="succeeded", result={"merge_sha": merge_sha})
            return ReconcileResult("adopted_merged_pr", f"PR #{pr_number} already merged ({merge_sha[:7]}).", {"merge_sha": merge_sha})
        if state == "OPEN":
            return ReconcileResult("proceed", f"PR #{pr_number} still open; safe to merge.", {"op_key": op_key})
        self.complete_remote_operation(op_key, status="ambiguous", result={"state": state})
        return ReconcileResult(
            "blocked",
            f"PR #{pr_number} in unexpected state '{state}'.",
            {"block_reason": "ambiguous_remote_operation"},
        )

    def reconcile_release_for_merge_sha(
        self,
        *,
        task_id: str,
        merge_sha: str,
        expected_asset_names: list[str],
        lease_token: str,
        existing_releases: list[dict[str, Any]],
    ) -> ReconcileResult:
        """Reconcile existing releases keyed by merge_sha; fail-closed on partial uploads."""
        op_key = f"release_create:{task_id}:{merge_sha}"
        self.begin_remote_operation(
            task_id=task_id,
            op_kind="release_create",
            idempotency_key=op_key,
            lease_token=lease_token,
            target_ref=merge_sha,
        )
        marker = f"<!-- nightshift-merge-sha: {merge_sha} -->"
        matching = [r for r in existing_releases if marker in str(r.get("body") or "") or r.get("merge_sha") == merge_sha]
        if not matching:
            return ReconcileResult("proceed", f"No release exists for {merge_sha[:7]}; safe to create.", {"op_key": op_key})

        rel = matching[0]
        present_assets = {str(a.get("name") if isinstance(a, dict) else a) for a in (rel.get("assets") or [])}
        required = set(expected_asset_names) | {"SHA256SUMS.txt"}
        missing = sorted(required - present_assets)
        if not missing:
            self.complete_remote_operation(op_key, status="succeeded", result={"tag": rel.get("tag")})
            return ReconcileResult(
                "adopted_release",
                f"Adopted complete release {rel.get('tag')} for {merge_sha[:7]}.",
                {"tag": rel.get("tag")},
            )
        self.complete_remote_operation(
            op_key,
            status="failed",
            result={"tag": rel.get("tag"), "missing_assets": missing},
        )
        return ReconcileResult(
            "blocked",
            f"Partial release {rel.get('tag')} is missing assets {missing}; refusing to overwrite tag/assets.",
            {"block_reason": "partial_release_repair", "tag": rel.get("tag"), "missing_assets": missing},
        )

    def recover_orphaned_running_tasks(
        self,
        *,
        is_pid_alive: Callable[[int], bool],
        checkpoint_fn: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[str]:
        """On startup, checkpoint and release leases for RUNNING tasks whose worker crashed or lease expired."""
        recovered: list[str] = []
        now = _utc_now()
        running = self.store.list_tasks(status="RUNNING")
        for task in running:
            pid = task.get("worker_pid")
            exp_raw = task.get("lease_expires_at")
            expired = False
            if exp_raw:
                try:
                    expired = datetime.fromisoformat(str(exp_raw)) <= now
                except ValueError:
                    expired = True
            dead = pid is not None and not is_pid_alive(int(pid))
            if dead or expired or pid is None:
                if checkpoint_fn is not None:
                    checkpoint_fn(task)
                with self.store.connection:
                    self.store.connection.execute(
                        """
                        UPDATE tasks
                        SET lease_token = NULL, lease_expires_at = NULL, worker_pid = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (_utc_iso(), task["id"]),
                    )
                recovered.append(str(task["id"]))
        return recovered
