"""Durable task state machine, remote_operations outbox, lease fencing, and crash/offline recovery.

Implements Governance Amendment D and docs/AGENT_PIPELINE.md section 4
(Phase 8c: Durable task orchestration).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

from nightshift.engine.pipeline_contracts import verify_contract_integrity
from nightshift.store import Store

logger = logging.getLogger(__name__)

_CHECKSUM_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
_LEASE_RECOVERY_STATUSES: tuple[str, ...] = (
    "QUEUED",
    "RUNNING",
    "VERIFYING",
    "REVIEWING",
    "APPROVED",
    "MERGED",
    "BLOCKED",
)


def _parse_release_body_checksums(body: str) -> dict[str, str]:
    """Parse ``<sha256>  <filename>`` lines from GitHub release notes."""
    checksums: dict[str, str] = {}
    for line in body.splitlines():
        m = _CHECKSUM_LINE_RE.match(line)
        if m:
            checksums[m.group(2).strip()] = m.group(1).lower()
    return checksums

T = TypeVar("T")

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

REMOTE_OP_KINDS: frozenset[str] = frozenset(
    {
        "pr_create",
        "pr_merge",
        "release_create",
        "release_asset_upload",
        "revert_pr_create",
    }
)

REMOTE_OP_STATUSES: frozenset[str] = frozenset({"pending", "succeeded", "ambiguous", "failed"})

# Columns permitted to be updated atomically during a state transition.
# Immutable identity/contract columns (id, phase_id, plan_id, branch, contract_id, contract_sha256)
# are intentionally excluded.
_TRANSITION_UPDATABLE_COLS: frozenset[str] = frozenset(
    {
        "status",
        "blocked_from_status",
        "risk_class",
        "active_attempt_id",
        "pr_number",
        "head_sha",
        "expected_base_sha",
        "ci_run_id",
        "review_sha",
        "approved_sha",
        "merge_sha",
        "merged_at",
        "release_tag",
        "smoke_verified_at",
        "owner_override_engine",
        "owner_approved_by",
        "owner_approved_sha",
        "owner_approved_at",
        "assertion_waiver_sha",
        "assertion_waiver_reason",
        "attempt_count",
        "ci_fix_rounds",
        "review_fix_rounds",
        "block_reason",
        "block_detail",
        "next_step",
        "failure_reason",
        "worker_pid",
        "lease_token",
        "lease_expires_at",
        "updated_at",
        "finished_at",
    }
)

NETWORK_BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120, 300)

_TRANSIENT_NETWORK_TOKENS: tuple[str, ...] = (
    "network",
    "dns",
    "could not resolve host",
    "temporary failure in name resolution",
    "connection reset",
    "connection refused",
    "connection timed out",
    "timed out",
    "timeout",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "tls handshake",
    "econnreset",
    "enetunreach",
    "getaddrinfo",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def network_backoff_delay(consecutive_errors: int) -> int:
    """Return exponential network-loss backoff delay (30s -> 60s -> 120s -> 300s cap)."""
    idx = max(0, min(consecutive_errors - 1, len(NETWORK_BACKOFF_SECONDS) - 1))
    return NETWORK_BACKOFF_SECONDS[idx]


class InvalidTransitionError(ValueError):
    """Raised when an illegal task state transition or invalid resume is attempted."""


class LeaseConflictError(RuntimeError):
    """Raised when a task lease is missing, expired, or held by another worker."""


class NetworkTransientError(RuntimeError):
    """Raised for transient network, DNS, or remote API connectivity failures."""


def is_transient_network_error(exc: BaseException) -> bool:
    """Return True if ``exc`` represents a transient network/DNS/API failure."""
    if isinstance(exc, (NetworkTransientError, ConnectionError, TimeoutError)):
        return True
    msg = str(exc).lower()
    return any(tok in msg for tok in _TRANSIENT_NETWORK_TOKENS)


def run_with_network_backoff(
    fn: Callable[[], T],
    *,
    max_retries: int = 4,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_backoff: Callable[[int, int, BaseException], None] | None = None,
) -> T:
    """Execute ``fn`` with exponential backoff on transient network errors without burning attempts."""
    consecutive = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if not is_transient_network_error(exc) or consecutive >= max_retries:
                raise
            consecutive += 1
            delay = network_backoff_delay(consecutive)
            if on_backoff is not None:
                on_backoff(consecutive, delay, exc)
            sleep_fn(delay)


async def async_run_with_network_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 4,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_backoff: Callable[[int, int, BaseException], None] | None = None,
) -> T:
    """Async variant of ``run_with_network_backoff``."""
    consecutive = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            if not is_transient_network_error(exc) or consecutive >= max_retries:
                raise
            consecutive += 1
            delay = network_backoff_delay(consecutive)
            if on_backoff is not None:
                on_backoff(consecutive, delay, exc)
            await sleep_fn(delay)


def is_process_alive(pid: int) -> bool:
    """Return True if a local process with ``pid`` is currently alive (Windows + POSIX)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            check = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(pid) in check.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a durable remote-operation reconciliation check."""

    action: str  # "proceed" | "adopted_open_pr" | "adopted_merged_pr" | "adopted_release" | "base_moved" | "blocked"
    detail: str
    data: dict[str, Any]


class TaskOrchestrator:
    """Crash-safe, idempotent task state machine backed by SQLite BEGIN IMMEDIATE transactions."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._memory_lock = threading.RLock()

    def _assert_active_lease_locked(
        self,
        task_row: Any,
        task_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        """Verify ``lease_token`` matches the active, non-expired lease inside an open transaction."""
        if not lease_token:
            raise LeaseConflictError(f"Missing lease_token for task {task_id}.")
        active_token = task_row["lease_token"]
        if not active_token or str(active_token) != lease_token:
            raise LeaseConflictError(
                f"Stale or missing lease_token for task {task_id}; active lease belongs to another worker."
            )
        exp_dt = _parse_iso(task_row["lease_expires_at"])
        if exp_dt is None or exp_dt <= now:
            raise LeaseConflictError(f"Lease for task {task_id} has expired at {task_row['lease_expires_at']}.")

    def transition(
        self,
        task_id: str,
        to_status: str,
        *,
        idempotency_key: str,
        reason: str,
        actor: str = "engine",
        updates: dict[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        """Atomically transition ``task_id`` to ``to_status`` and record ``task_transitions``.

        Idempotent on ``idempotency_key``: replaying an already-applied key for the same task
        returns the current task without duplicating transitions or side effects.
        """
        if to_status not in TASK_STATES:
            raise InvalidTransitionError(f"Unknown target state: {to_status}")

        extra: dict[str, Any] = dict(updates or {})
        disallowed = [k for k in extra if k not in _TRANSITION_UPDATABLE_COLS]
        if disallowed:
            raise InvalidTransitionError(f"Disallowed task column update(s) in transition: {disallowed}")

        conn = self.store.connection
        now_dt = _utc_now()
        now = _utc_iso(now_dt)
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_tx = conn.execute(
                "SELECT * FROM task_transitions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_tx is not None:
                if str(existing_tx["task_id"]) != task_id:
                    raise InvalidTransitionError(
                        f"Idempotency key '{idempotency_key}' already used by task {existing_tx['task_id']}."
                    )
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                conn.commit()
                if row is None:
                    raise KeyError(f"Unknown task: {task_id}")
                return dict(row)

            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")

            if lease_token is not None:
                self._assert_active_lease_locked(row, task_id, lease_token, now_dt)

            from_status = str(row["status"])
            if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
                raise InvalidTransitionError(f"Illegal transition {from_status} -> {to_status} for task {task_id}")

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

            if from_status == "FAILED" and to_status == "QUEUED":
                extra.setdefault("failure_reason", "")
                extra.setdefault("finished_at", None)

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
        """Atomically acquire a rotated fenced lease token for ``task_id``."""
        conn = self.store.connection
        now = _utc_now()
        now_iso = _utc_iso(now)
        expires_iso = _utc_iso(now + timedelta(seconds=lease_seconds))
        token = str(uuid.uuid4())
        pid = worker_pid if worker_pid is not None else os.getpid()

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT lease_token, lease_expires_at, worker_pid FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            existing_token = row["lease_token"]
            existing_exp = _parse_iso(row["lease_expires_at"])
            if existing_token and existing_exp is not None and existing_exp > now:
                raise LeaseConflictError(f"Task {task_id} already leased until {row['lease_expires_at']}")
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

    def renew_lease(
        self,
        task_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 1800,
    ) -> str:
        """Extend an active lease's expiry inside ``BEGIN IMMEDIATE``; fails if fenced out.

        Uses a dedicated short-lived WAL connection for file-backed SQLite stores so
        background heartbeat threads can renew leases safely even while the main thread
        runs blocking workflow watches or writes to ``self.store.connection``.
        """
        now = _utc_now()
        now_iso = _utc_iso(now)
        expires_iso = _utc_iso(now + timedelta(seconds=lease_seconds))

        db_path_str = str(self.store.db_path)
        own_conn = db_path_str != ":memory:"
        if own_conn:
            conn = sqlite3.connect(db_path_str, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000;")
        else:
            conn = self.store.connection

        def _do_renew() -> str:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT lease_token, lease_expires_at FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown task: {task_id}")
                self._assert_active_lease_locked(row, task_id, lease_token, now)
                conn.execute(
                    "UPDATE tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ? AND lease_token = ?",
                    (expires_iso, now_iso, task_id, lease_token),
                )
                conn.commit()
                return expires_iso
            except Exception:
                conn.rollback()
                raise

        try:
            if own_conn:
                return _do_renew()
            with self._memory_lock:
                return _do_renew()
        finally:
            if own_conn:
                conn.close()

    def verify_lease(self, task_id: str, lease_token: str) -> None:
        """Raise ``LeaseConflictError`` if ``lease_token`` is not the active non-expired lease."""
        row = self.store.connection.execute(
            "SELECT lease_token, lease_expires_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        self._assert_active_lease_locked(row, task_id, lease_token, _utc_now())

    def release_lease(self, task_id: str, lease_token: str) -> bool:
        """Release the lease only if ``lease_token`` still owns it."""
        conn = self.store.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                UPDATE tasks
                SET lease_token = NULL, lease_expires_at = NULL, worker_pid = NULL, updated_at = ?
                WHERE id = ? AND lease_token = ?
                """,
                (_utc_iso(), task_id, lease_token),
            )
            released = cur.rowcount > 0
            conn.commit()
            return released
        except Exception:
            conn.rollback()
            raise

    def record_contributing_provider(self, task_id: str, provider: str) -> list[str]:
        """Persist ``provider`` in ``tasks.contributing_providers_json`` (idempotent set)."""
        norm = (provider or "").strip().lower()
        conn = self.store.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT contributing_providers_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            providers = list(json.loads(str(row["contributing_providers_json"] or "[]")))
            if norm and norm not in providers:
                providers.append(norm)
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
        current_merge_sha: str | None = None,
        owner_approved_by: str | None = None,
        restart_to_queued: bool = False,
        actor: str = "owner",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resume a BLOCKED task to ``blocked_from_status`` (or APPROVED) after full revalidation.

        Revalidates (AGENT_PIPELINE §4):
        1. ``contract_sha256`` matches the frozen contract row,
        2. ``head_sha`` (or ``merge_sha``) has not drifted,
        3. required ``evidence_records`` are still valid for that exact SHA & contract, and
        4. ``owner_approved_sha == head_sha`` when owner approval is required.

        Only ``restart_to_queued=True`` (explicit owner restart / contract revision) or
        ``blocked_from_status`` in ``QUEUED``/``RUNNING`` returns the task to ``QUEUED``.
        """
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        if task["status"] != "BLOCKED":
            raise InvalidTransitionError(f"Task {task_id} is {task['status']}, not BLOCKED")

        contract = self.store.get_task_contract(str(task["contract_id"]))
        if contract is None:
            raise InvalidTransitionError(f"Missing frozen contract {task['contract_id']} for task {task_id}")

        ok_contract, contract_msg = verify_contract_integrity(
            contract,
            expected_sha256=str(task["contract_sha256"]),
        )
        if not ok_contract:
            raise InvalidTransitionError(f"Cannot resume task {task_id}: {contract_msg}")

        blocked_from = str(task.get("blocked_from_status") or "")
        if restart_to_queued:
            key = idempotency_key or f"resume:{task_id}:restart_queued:{_utc_iso()}"
            return self.transition(
                task_id,
                "QUEUED",
                idempotency_key=key,
                reason="Owner restarted task to QUEUED",
                actor=actor,
            )

        expected_head = str(task.get("head_sha") or "")
        expected_merge = str(task.get("merge_sha") or "")

        # Verify head_sha has not drifted for post-coding stages
        if blocked_from in ("VERIFYING", "REVIEWING", "APPROVED"):
            if current_head_sha and expected_head and current_head_sha != expected_head:
                raise InvalidTransitionError(
                    f"Cannot resume task {task_id}: head_sha drifted ({expected_head[:7]} -> {current_head_sha[:7]})."
                )
        # Verify merge_sha has not drifted for post-merge stage
        if blocked_from == "MERGED":
            if current_merge_sha and expected_merge and current_merge_sha != expected_merge:
                raise InvalidTransitionError(
                    f"Cannot resume task {task_id}: merge_sha drifted ({expected_merge[:7]} -> {current_merge_sha[:7]})."
                )

        updates: dict[str, Any] = {}
        if owner_approved_by and expected_head:
            updates["owner_approved_by"] = owner_approved_by
            updates["owner_approved_sha"] = expected_head
            updates["owner_approved_at"] = _utc_iso()

        risk_class = str(task.get("risk_class") or contract.get("risk_class") or "")
        owner_required = risk_class in ("governance", "high_risk") or bool(contract.get("owner_approval_required"))
        effective_approved_sha = str(updates.get("owner_approved_sha") or task.get("owner_approved_sha") or "")

        if blocked_from in ("REVIEWING", "APPROVED"):
            if not expected_head:
                raise InvalidTransitionError(
                    f"Cannot resume task {task_id} from {blocked_from}: missing persisted head_sha."
                )
            evidence = self.store.list_evidence_records(task_id, commit_sha=expected_head)
            has_valid_ci = any(
                e.get("kind") == "ci_check"
                and e.get("contract_sha256") == task["contract_sha256"]
                and bool(e.get("verified"))
                for e in evidence
            )
            approval_satisfied = (not owner_required) or (effective_approved_sha == expected_head)

            if blocked_from == "APPROVED":
                if not has_valid_ci:
                    raise InvalidTransitionError(
                        f"Cannot resume task {task_id} to APPROVED: verified CI evidence missing for {expected_head[:7]}."
                    )
                if not approval_satisfied:
                    raise InvalidTransitionError(
                        f"Cannot resume task {task_id} to APPROVED: owner approval missing for {expected_head[:7]}."
                    )
                target = "APPROVED"
                updates["approved_sha"] = expected_head
            else:
                # blocked_from == "REVIEWING"
                if owner_approved_by or (owner_required and approval_satisfied):
                    if not has_valid_ci:
                        raise InvalidTransitionError(
                            f"Cannot resume task {task_id} to APPROVED: verified CI evidence missing for {expected_head[:7]}."
                        )
                    if not approval_satisfied:
                        raise InvalidTransitionError(
                            f"Cannot resume task {task_id} to APPROVED: owner approval missing for {expected_head[:7]}."
                        )
                    target = "APPROVED"
                    updates["approved_sha"] = expected_head
                elif has_valid_ci and approval_satisfied and str(task.get("review_sha") or "") == expected_head:
                    target = "APPROVED"
                    updates["approved_sha"] = expected_head
                else:
                    target = "REVIEWING"
        elif blocked_from == "VERIFYING":
            target = "VERIFYING"
        elif blocked_from == "MERGED":
            target = "MERGED"
        else:
            target = "QUEUED"

        key = idempotency_key or f"resume:{task_id}:{blocked_from or 'unknown'}:{target}:{_utc_iso()}"
        return self.transition(
            task_id,
            target,
            idempotency_key=key,
            reason=f"Resumed from BLOCKED ({blocked_from or 'QUEUED'} -> {target}) after revalidation",
            actor=actor,
            updates=updates,
        )

    def retry_failed_task(
        self,
        task_id: str,
        *,
        actor: str = "owner",
        reset_counters: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly retry a FAILED task with fresh budget (FAILED -> QUEUED)."""
        updates: dict[str, Any] = {}
        if reset_counters:
            updates["attempt_count"] = 0
            updates["ci_fix_rounds"] = 0
            updates["review_fix_rounds"] = 0
        key = idempotency_key or f"retry:{task_id}:{_utc_iso()}"
        return self.transition(
            task_id,
            "QUEUED",
            idempotency_key=key,
            reason="Owner explicitly retried FAILED task with fresh budget",
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
        """Write-ahead intent into ``remote_operations`` fenced by active ``lease_token``."""
        if op_kind not in REMOTE_OP_KINDS:
            raise ValueError(f"Unsupported remote operation kind: {op_kind}")

        conn = self.store.connection
        now_dt = _utc_now()
        now = _utc_iso(now_dt)
        conn.execute("BEGIN IMMEDIATE")
        try:
            task = conn.execute(
                "SELECT lease_token, lease_expires_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(f"Unknown task: {task_id}")
            self._assert_active_lease_locked(task, task_id, lease_token, now_dt)

            existing = conn.execute(
                "SELECT * FROM remote_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["status"]) != "succeeded":
                    conn.execute(
                        "UPDATE remote_operations SET lease_token = ?, updated_at = ? WHERE idempotency_key = ?",
                        (lease_token, now, idempotency_key),
                    )
                    existing = conn.execute(
                        "SELECT * FROM remote_operations WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                conn.commit()
                assert existing is not None
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
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        """Update a ``remote_operations`` row; optionally verifies active ``lease_token``."""
        if status not in REMOTE_OP_STATUSES:
            raise ValueError(f"Unsupported remote operation status: {status}")

        conn = self.store.connection
        now_dt = _utc_now()
        now = _utc_iso(now_dt)
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM remote_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"Unknown remote operation: {idempotency_key}")
            if lease_token is not None:
                task = conn.execute(
                    "SELECT lease_token, lease_expires_at FROM tasks WHERE id = ?",
                    (existing["task_id"],),
                ).fetchone()
                if task is None:
                    raise KeyError(f"Unknown task: {existing['task_id']}")
                self._assert_active_lease_locked(task, str(existing["task_id"]), lease_token, now_dt)
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
            conn.commit()
            assert row is not None
            return dict(row)
        except Exception:
            conn.rollback()
            raise

    def reconcile_pr_create(
        self,
        *,
        task_id: str,
        branch: str,
        lease_token: str,
        existing_prs: list[dict[str, Any]] | None = None,
        pr_list_lookup: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> ReconcileResult:
        """Reconcile PR state (open/merged/closed) before creating a PR so crashes never duplicate PRs."""
        op_key = f"pr_create:{task_id}:{branch}"
        self.begin_remote_operation(
            task_id=task_id,
            op_kind="pr_create",
            idempotency_key=op_key,
            lease_token=lease_token,
            target_ref=branch,
        )
        if existing_prs is None:
            if pr_list_lookup is None:
                raise ValueError("Either existing_prs or pr_list_lookup must be provided")
            try:
                prs = list(pr_list_lookup())
            except Exception as exc:
                self.complete_remote_operation(
                    op_key,
                    status="ambiguous",
                    result={"error": str(exc)},
                    lease_token=lease_token,
                )
                return ReconcileResult(
                    "blocked",
                    f"PR lookup ambiguous due to API error: {exc}",
                    {"block_reason": "ambiguous_remote_operation", "op_key": op_key},
                )
        else:
            prs = list(existing_prs)

        open_prs = [p for p in prs if str(p.get("state", "")).upper() == "OPEN"]
        merged_prs = [p for p in prs if str(p.get("state", "")).upper() == "MERGED"]
        closed_prs = [p for p in prs if str(p.get("state", "")).upper() == "CLOSED"]

        if open_prs:
            pr = open_prs[0]
            self.complete_remote_operation(op_key, status="succeeded", result=pr, lease_token=lease_token)
            return ReconcileResult("adopted_open_pr", f"Adopted existing open PR #{pr.get('number')}", pr)
        if merged_prs:
            pr = merged_prs[0]
            self.complete_remote_operation(op_key, status="succeeded", result=pr, lease_token=lease_token)
            return ReconcileResult("adopted_merged_pr", f"Adopted already-merged PR #{pr.get('number')}", pr)
        if closed_prs:
            pr = closed_prs[0]
            self.complete_remote_operation(op_key, status="failed", result=pr, lease_token=lease_token)
            return ReconcileResult(
                "blocked",
                f"Closed unmerged PR #{pr.get('number')} exists for branch {branch}; refusing to duplicate.",
                {"block_reason": "closed_pr_exists", "pr": pr, "op_key": op_key},
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
        expected_base_sha: str | None = None,
        current_base_sha: str | None = None,
    ) -> ReconcileResult:
        """Reconcile PR merge state & race-safe base branch check; fail closed on ambiguity."""
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
            self.complete_remote_operation(
                op_key,
                status="ambiguous",
                result={"error": str(exc)},
                lease_token=lease_token,
            )
            return ReconcileResult(
                "blocked",
                f"Merge status ambiguous due to API error: {exc}",
                {"block_reason": "ambiguous_remote_operation", "op_key": op_key},
            )
        if pr is None:
            self.complete_remote_operation(
                op_key,
                status="ambiguous",
                result={"error": "PR not found"},
                lease_token=lease_token,
            )
            return ReconcileResult(
                "blocked",
                f"Merge status ambiguous: PR #{pr_number} not found.",
                {"block_reason": "ambiguous_remote_operation", "op_key": op_key},
            )
        state = str(pr.get("state", "")).upper()
        if state == "MERGED":
            merge_sha = str((pr.get("mergeCommit") or {}).get("oid") or pr.get("merge_sha") or "")
            self.complete_remote_operation(
                op_key,
                status="succeeded",
                result={"merge_sha": merge_sha},
                lease_token=lease_token,
            )
            return ReconcileResult(
                "adopted_merged_pr",
                f"PR #{pr_number} already merged ({merge_sha[:7]}).",
                {"merge_sha": merge_sha, "op_key": op_key},
            )
        if state == "OPEN":
            if expected_base_sha and current_base_sha and expected_base_sha != current_base_sha:
                self.complete_remote_operation(
                    op_key,
                    status="failed",
                    result={"expected_base_sha": expected_base_sha, "current_base_sha": current_base_sha},
                    lease_token=lease_token,
                )
                return ReconcileResult(
                    "base_moved",
                    f"Base branch moved ({expected_base_sha[:7]} -> {current_base_sha[:7]}); update branch and re-verify.",
                    {
                        "expected_base_sha": expected_base_sha,
                        "current_base_sha": current_base_sha,
                        "op_key": op_key,
                    },
                )
            head_oid = str(pr.get("headRefOid") or "")
            if head_oid and head_oid != approved_sha:
                self.complete_remote_operation(
                    op_key,
                    status="failed",
                    result={"approved_sha": approved_sha, "head_oid": head_oid},
                    lease_token=lease_token,
                )
                return ReconcileResult(
                    "blocked",
                    f"PR #{pr_number} head SHA drifted ({approved_sha[:7]} -> {head_oid[:7]}).",
                    {"block_reason": "sha_drift", "head_oid": head_oid, "op_key": op_key},
                )
            return ReconcileResult("proceed", f"PR #{pr_number} still open; safe to merge.", {"op_key": op_key})

        self.complete_remote_operation(
            op_key,
            status="ambiguous",
            result={"state": state},
            lease_token=lease_token,
        )
        return ReconcileResult(
            "blocked",
            f"PR #{pr_number} in unexpected state '{state}'.",
            {"block_reason": "ambiguous_remote_operation", "op_key": op_key},
        )

    def reconcile_release_for_merge_sha(
        self,
        *,
        task_id: str,
        merge_sha: str,
        expected_asset_names: list[str],
        lease_token: str,
        existing_releases: list[dict[str, Any]] | None = None,
        release_list_lookup: Callable[[], list[dict[str, Any]]] | None = None,
        expected_checksums: dict[str, str] | None = None,
        expected_asset_globs: list[str] | None = None,
    ) -> ReconcileResult:
        """Reconcile release creation & asset upload keyed by ``merge_sha``; fail closed on partials."""
        create_op_key = f"release_create:{task_id}:{merge_sha}"
        asset_op_key = f"release_asset_upload:{task_id}:{merge_sha}"
        self.begin_remote_operation(
            task_id=task_id,
            op_kind="release_create",
            idempotency_key=create_op_key,
            lease_token=lease_token,
            target_ref=merge_sha,
        )
        self.begin_remote_operation(
            task_id=task_id,
            op_kind="release_asset_upload",
            idempotency_key=asset_op_key,
            lease_token=lease_token,
            target_ref=merge_sha,
        )

        if existing_releases is None:
            if release_list_lookup is None:
                raise ValueError("Either existing_releases or release_list_lookup must be provided")
            try:
                releases = list(release_list_lookup())
            except Exception as exc:
                self.complete_remote_operation(
                    create_op_key,
                    status="ambiguous",
                    result={"error": str(exc)},
                    lease_token=lease_token,
                )
                self.complete_remote_operation(
                    asset_op_key,
                    status="ambiguous",
                    result={"error": str(exc)},
                    lease_token=lease_token,
                )
                return ReconcileResult(
                    "blocked",
                    f"Release lookup ambiguous due to API error: {exc}",
                    {"block_reason": "ambiguous_remote_operation", "op_key": create_op_key},
                )
        else:
            releases = list(existing_releases)

        marker = f"<!-- nightshift-merge-sha: {merge_sha} -->"
        matching = [
            r
            for r in releases
            if marker in str(r.get("body") or r.get("notes") or "") or r.get("merge_sha") == merge_sha
        ]
        if not matching:
            return ReconcileResult(
                "proceed",
                f"No release exists for {merge_sha[:7]}; safe to create.",
                {"op_key": create_op_key, "asset_op_key": asset_op_key},
            )

        rel = matching[0]
        tag = str(rel.get("tag") or rel.get("tag_name") or "")
        present_assets = {str(a.get("name") if isinstance(a, dict) else a) for a in (rel.get("assets") or [])}
        required = set(expected_asset_names) | {"SHA256SUMS.txt"}
        missing = sorted(required - present_assets)
        for pattern in expected_asset_globs or ():
            if pattern and not any(
                name != "SHA256SUMS.txt" and fnmatch.fnmatch(name, pattern)
                for name in present_assets
            ):
                missing.append(pattern)
        if not expected_asset_names and not expected_asset_globs:
            if not any(name != "SHA256SUMS.txt" for name in present_assets):
                missing.append("<application_artifact>")

        checksum_mismatches: list[str] = []
        if not missing and expected_checksums:
            remote_checksums = {
                str(k): str(v).lower() for k, v in dict(rel.get("checksums") or {}).items()
            }
            if not remote_checksums:
                remote_checksums = _parse_release_body_checksums(
                    str(rel.get("body") or rel.get("notes") or "")
                )
            for fname, exp_sha in expected_checksums.items():
                if remote_checksums.get(fname) != str(exp_sha).lower():
                    checksum_mismatches.append(fname)

        if not missing and not checksum_mismatches:
            self.complete_remote_operation(
                create_op_key,
                status="succeeded",
                result={"tag": tag},
                lease_token=lease_token,
            )
            self.complete_remote_operation(
                asset_op_key,
                status="succeeded",
                result={"tag": tag, "assets": sorted(present_assets)},
                lease_token=lease_token,
            )
            return ReconcileResult(
                "adopted_release",
                f"Adopted complete release {tag} for {merge_sha[:7]}.",
                {"tag": tag, "op_key": create_op_key, "asset_op_key": asset_op_key},
            )

        # Partial release or checksum mismatch: NEVER overwrite existing tag/assets; fail closed.
        self.complete_remote_operation(
            create_op_key,
            status="succeeded",
            result={"tag": tag, "partial": True},
            lease_token=lease_token,
        )
        self.complete_remote_operation(
            asset_op_key,
            status="failed",
            result={"tag": tag, "missing_assets": missing, "checksum_mismatches": checksum_mismatches},
            lease_token=lease_token,
        )
        return ReconcileResult(
            "blocked",
            f"Partial release {tag} is missing assets {missing} or failed checksum check {checksum_mismatches}; "
            f"refusing to overwrite existing tag/assets.",
            {
                "block_reason": "partial_release_repair",
                "tag": tag,
                "missing_assets": missing,
                "checksum_mismatches": checksum_mismatches,
                "op_key": create_op_key,
                "asset_op_key": asset_op_key,
            },
        )

    def recover_orphaned_running_tasks(
        self,
        *,
        is_pid_alive: Callable[[int], bool] = is_process_alive,
        checkpoint_fn: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[str]:
        """On startup, recover dead-worker or expired leases across all active execution stages.

        - ``RUNNING`` tasks with a dead worker, expired lease, or missing worker PID are
          WIP-checkpointed (when ``checkpoint_fn`` is provided) and have their lease cleared.
        - Post-coding / non-coding tasks (``QUEUED``, ``VERIFYING``, ``REVIEWING``,
          ``APPROVED``, ``MERGED``, ``BLOCKED``) that still hold a dead or expired lease
          have their lease cleared without WIP-checkpointing so PR heads and merged
          branches are never mutated and resume does not have to wait for lease expiry.
        - Tasks in post-coding / non-coding states with no attached lease are skipped.
        """
        recovered: list[str] = []
        now = _utc_now()
        candidate_tasks = [
            task for task in self.store.list_tasks()
            if task.get("status") in _LEASE_RECOVERY_STATUSES
        ]
        for task in candidate_tasks:
            status = str(task.get("status") or "")
            pid = task.get("worker_pid")
            expected_token = task.get("lease_token")
            exp_raw = task.get("lease_expires_at")
            has_attached_lease = expected_token is not None or pid is not None or exp_raw is not None

            if status != "RUNNING" and not has_attached_lease:
                continue

            exp_dt = _parse_iso(exp_raw)
            expired = exp_dt is None or exp_dt <= now
            dead = pid is not None and not is_pid_alive(int(pid))
            if not (dead or expired or pid is None):
                continue

            if status == "RUNNING" and checkpoint_fn is not None:
                checkpoint_fn(task)

            conn = self.store.connection
            conn.execute("BEGIN IMMEDIATE")
            try:
                if expected_token is None:
                    cur = conn.execute(
                        """
                        UPDATE tasks
                        SET lease_token = NULL, lease_expires_at = NULL, worker_pid = NULL, updated_at = ?
                        WHERE id = ? AND status = ? AND lease_token IS NULL
                        """,
                        (_utc_iso(), task["id"], status),
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE tasks
                        SET lease_token = NULL, lease_expires_at = NULL, worker_pid = NULL, updated_at = ?
                        WHERE id = ? AND status = ? AND lease_token = ?
                        """,
                        (_utc_iso(), task["id"], status, expected_token),
                    )
                conn.commit()
                if cur.rowcount > 0:
                    recovered.append(str(task["id"]))
            except Exception:
                conn.rollback()
                raise
        return recovered


class LeaseHeartbeat:
    """Background daemon thread that continuously renews a task lease across async and blocking stages."""

    def __init__(
        self,
        orchestrator: TaskOrchestrator,
        task_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 1800,
        interval_seconds: float | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.task_id = task_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: BaseException | None = None

    def _effective_interval(self) -> float:
        if self.interval_seconds is not None:
            return max(0.05, float(self.interval_seconds))
        return max(0.05, min(self.lease_seconds / 3.0, 300.0))

    def _renew_once(self) -> bool:
        try:
            self.orchestrator.renew_lease(
                self.task_id,
                self.lease_token,
                lease_seconds=self.lease_seconds,
            )
            return True
        except (LeaseConflictError, KeyError) as exc:
            self.last_error = exc
            logger.warning("Stopping lease heartbeat for task %s: %s", self.task_id, exc)
            return False
        except Exception as exc:
            self.last_error = exc
            logger.warning("Transient lease renewal failure for task %s: %s", self.task_id, exc)
            return True

    def _run(self, wait_interval: float) -> None:
        while not self._stop_event.wait(wait_interval):
            if not self._renew_once():
                break

    def start(self) -> LeaseHeartbeat:
        if self._thread is not None:
            return self
        self._stop_event.clear()
        wait_interval = self._effective_interval()
        # If the initial lease is already near expiry (e.g., short lease TTL), renew promptly.
        task_row = self.orchestrator.store.get_task(self.task_id)
        if task_row is not None:
            exp_dt = _parse_iso(task_row.get("lease_expires_at"))
            if exp_dt is not None:
                remaining = (exp_dt - _utc_now()).total_seconds()
                if remaining < 30.0:
                    self._renew_once()
                elif self.interval_seconds is None:
                    wait_interval = min(wait_interval, max(0.05, remaining / 3.0))
        self._thread = threading.Thread(
            target=self._run,
            args=(wait_interval,),
            name=f"lease-heartbeat-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> LeaseHeartbeat:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
