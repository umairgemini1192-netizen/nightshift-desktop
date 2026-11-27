"""SQLite persistence layer with WAL mode, migrations, and hash-chained audit events.

Schema implements BLUEPRINT section 6 in full so every engine step, router
decision, handoff, login check, guard result, and event is durable across restarts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MigrationFn = Callable[[sqlite3.Connection], None]

GENESIS_HASH = "0" * 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_event_hash(
    prev_hash: str,
    ts: str,
    level: str,
    phase_id: str | None,
    kind: str,
    plain_text: str,
    raw_path: str | None,
) -> str:
    payload = "|".join(
        [
            prev_hash,
            ts,
            level,
            phase_id or "",
            kind,
            plain_text,
            raw_path or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _migration_001_initial_schema(conn: sqlite3.Connection) -> None:
    """Create all core tables specified in BLUEPRINT section 6."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            repo TEXT NOT NULL,
            local_path TEXT NOT NULL,
            build_rules TEXT NOT NULL DEFAULT '',
            ui_contract TEXT NOT NULL DEFAULT '',
            release_workflow TEXT NOT NULL DEFAULT 'Android build',
            artifact_glob TEXT NOT NULL DEFAULT '',
            artifact_prefix TEXT NOT NULL DEFAULT '',
            tag_prefix TEXT NOT NULL DEFAULT 'v1.0.0-preview.',
            output_folder TEXT NOT NULL DEFAULT '',
            output_dir TEXT NOT NULL DEFAULT '',
            critical_paths_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS phases (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '',
            depends_on_json TEXT NOT NULL DEFAULT '[]',
            ladder TEXT NOT NULL DEFAULT 'default',
            size TEXT NOT NULL DEFAULT 'medium',
            timeout_minutes INTEGER NOT NULL DEFAULT 180,
            auto_merge INTEGER NOT NULL DEFAULT 1,
            release_after_merge INTEGER NOT NULL DEFAULT 0,
            force_rerun INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt INTEGER NOT NULL DEFAULT 0,
            pr_number INTEGER,
            branch TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            runner TEXT NOT NULL,
            rc INTEGER,
            verified INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS engines (
            id TEXT PRIMARY KEY,
            runner TEXT NOT NULL,
            model TEXT NOT NULL,
            effort TEXT NOT NULL DEFAULT 'medium',
            skills_json TEXT NOT NULL DEFAULT '{}',
            cost_tier TEXT NOT NULL DEFAULT 'standard',
            budget_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            pinned INTEGER NOT NULL DEFAULT 0,
            command_template TEXT NOT NULL DEFAULT '[]',
            usage_parser TEXT NOT NULL DEFAULT '',
            quota_patterns_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS router_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            step_id INTEGER,
            chosen TEXT NOT NULL,
            reason TEXT NOT NULL,
            scores_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (step_id) REFERENCES steps(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS phase_assignments (
            phase_id TEXT PRIMARY KEY,
            planned_engine TEXT,
            override_engine TEXT,
            model TEXT,
            effort TEXT NOT NULL DEFAULT 'medium',
            FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS priority_list (
            position INTEGER PRIMARY KEY,
            engine TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            effort TEXT NOT NULL DEFAULT 'medium'
        );

        CREATE TABLE IF NOT EXISTS guard_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase_id TEXT NOT NULL,
            guard TEXT NOT NULL,
            passed INTEGER NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS handoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase_id TEXT NOT NULL,
            from_engine TEXT NOT NULL,
            to_engine TEXT NOT NULL,
            reason TEXT NOT NULL,
            checkpoint_sha TEXT NOT NULL,
            ts TEXT NOT NULL,
            FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS parked (
            phase_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            next_step TEXT NOT NULL,
            stacked_children_json TEXT NOT NULL DEFAULT '[]',
            parked_at TEXT NOT NULL,
            FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS login_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool TEXT NOT NULL,
            state TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            back_at TEXT,
            checked_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS engine_stats (
            engine TEXT NOT NULL,
            size TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            minutes REAL NOT NULL DEFAULT 0.0,
            est_cost REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (engine, size)
        );

        CREATE TABLE IF NOT EXISTS council_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            role TEXT NOT NULL,
            engine TEXT NOT NULL,
            input TEXT NOT NULL DEFAULT '',
            output TEXT NOT NULL DEFAULT '',
            usage_estimate TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS runner_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            runner TEXT NOT NULL,
            model TEXT NOT NULL,
            event TEXT NOT NULL,
            reset_at TEXT,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS durations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            runner TEXT NOT NULL,
            seconds REAL NOT NULL,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            phase_id TEXT,
            kind TEXT NOT NULL,
            plain_text TEXT NOT NULL,
            raw_path TEXT,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_phases_plan_id ON phases(plan_id);
        CREATE INDEX IF NOT EXISTS idx_steps_phase_id ON steps(phase_id);
        CREATE INDEX IF NOT EXISTS idx_events_phase_id ON events(phase_id);
        CREATE INDEX IF NOT EXISTS idx_login_checks_tool ON login_checks(tool, checked_at);
        CREATE INDEX IF NOT EXISTS idx_runner_ledger_runner ON runner_ledger(runner, created_at);
        """
    )


def _migration_002_project_columns(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before the project release settings existed."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    for name, definition in (
        ("ui_contract", "TEXT NOT NULL DEFAULT ''"),
        ("release_workflow", "TEXT NOT NULL DEFAULT 'Android build'"),
        ("artifact_prefix", "TEXT NOT NULL DEFAULT ''"),
        ("tag_prefix", "TEXT NOT NULL DEFAULT 'v1.0.0-preview.'"),
        ("output_dir", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
    conn.execute("UPDATE projects SET output_dir = output_folder WHERE output_dir = ''")


def _migration_003_nullable_router_step(conn: sqlite3.Connection) -> None:
    """Allow decisions made before a step exists while preserving prior decisions."""
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(router_decisions)")}
    if not columns or not columns["step_id"][3]:
        return
    conn.execute("""CREATE TABLE router_decisions_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        step_id INTEGER,
        chosen TEXT NOT NULL,
        reason TEXT NOT NULL,
        scores_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY (step_id) REFERENCES steps(id) ON DELETE CASCADE
    )""")
    conn.execute("""INSERT INTO router_decisions_new
        (id, step_id, chosen, reason, scores_json, created_at)
        SELECT id, step_id, chosen, reason, scores_json, created_at FROM router_decisions""")
    conn.execute("DROP TABLE router_decisions")
    conn.execute("ALTER TABLE router_decisions_new RENAME TO router_decisions")


def _migration_004_token_optimizer_tables(conn: sqlite3.Connection) -> None:
    """Create tables for token optimizer tool states and A/B measurement runs (BLUEPRINT 5g)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_tool_states (
            tool TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            installed_version TEXT NOT NULL DEFAULT '',
            sha256_verified INTEGER NOT NULL DEFAULT 0,
            isolation_passed INTEGER NOT NULL DEFAULT 0,
            isolation_method TEXT NOT NULL DEFAULT '',
            auto_disabled INTEGER NOT NULL DEFAULT 0,
            warning TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS token_ab_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            phase_kind TEXT NOT NULL DEFAULT 'default',
            engine TEXT NOT NULL DEFAULT '',
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            turns INTEGER NOT NULL DEFAULT 1,
            retry_turns INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_token_ab_runs_tool_kind
            ON token_ab_runs(tool, phase_kind, enabled);
        """
    )


def _migration_005_agent_pipeline(conn: sqlite3.Connection) -> None:
    """Create tables and immutability triggers for the role-based agent pipeline (AGENT_PIPELINE §5)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            phase_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            title TEXT NOT NULL,
            branch TEXT NOT NULL UNIQUE,
            risk_class TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            blocked_from_status TEXT NOT NULL DEFAULT '',
            contract_id TEXT NOT NULL,
            contract_sha256 TEXT NOT NULL,
            active_attempt_id INTEGER,
            pr_number INTEGER,
            head_sha TEXT,
            expected_base_sha TEXT,
            ci_run_id TEXT,
            review_sha TEXT,
            approved_sha TEXT,
            merge_sha TEXT,
            merged_at TEXT,
            release_tag TEXT,
            smoke_verified_at TEXT,
            contributing_providers_json TEXT NOT NULL DEFAULT '[]',
            owner_override_engine TEXT,
            owner_approved_by TEXT,
            owner_approved_sha TEXT,
            owner_approved_at TEXT,
            assertion_waiver_sha TEXT,
            assertion_waiver_reason TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            ci_fix_rounds INTEGER NOT NULL DEFAULT 0,
            review_fix_rounds INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT NOT NULL DEFAULT '',
            block_detail TEXT NOT NULL DEFAULT '',
            next_step TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            worker_pid INTEGER,
            lease_token TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE,
            FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_contracts (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            risk_class TEXT NOT NULL,
            risk_factors_json TEXT NOT NULL,
            acceptance_criteria_json TEXT NOT NULL,
            allowed_globs_json TEXT NOT NULL,
            forbidden_globs_json TEXT NOT NULL,
            budget_json TEXT NOT NULL,
            owner_approval_required INTEGER NOT NULL DEFAULT 0,
            canonical_json TEXT NOT NULL,
            contract_sha256 TEXT NOT NULL,
            frozen_at TEXT NOT NULL,
            UNIQUE (task_id, version),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TRIGGER IF NOT EXISTS trg_task_contracts_no_update
        BEFORE UPDATE ON task_contracts
        BEGIN
            SELECT RAISE(ABORT, 'task_contracts rows are immutable; insert a new version instead');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_task_contracts_no_delete
        BEFORE DELETE ON task_contracts
        BEGIN
            SELECT RAISE(ABORT, 'task_contracts rows are append-only and cannot be deleted');
        END;

        CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            role TEXT NOT NULL,
            engine_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            input_ref TEXT NOT NULL,
            output_ref TEXT NOT NULL,
            decision_reason TEXT NOT NULL DEFAULT '',
            head_sha_before TEXT,
            head_sha_after TEXT,
            rc INTEGER,
            outcome TEXT NOT NULL,
            wall_seconds REAL NOT NULL DEFAULT 0.0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS remote_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            op_kind TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            lease_token TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS pipeline_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_id INTEGER,
            role TEXT NOT NULL,
            engine_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            risk_class TEXT NOT NULL,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            est_token_cost_usd REAL NOT NULL DEFAULT 0.0,
            ci_minutes REAL NOT NULL DEFAULT 0.0,
            artifact_mb REAL NOT NULL DEFAULT 0.0,
            wall_seconds REAL NOT NULL DEFAULT 0.0,
            outcome TEXT NOT NULL,
            is_estimate INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (attempt_id) REFERENCES task_attempts(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS model_scores (
            engine_id TEXT NOT NULL,
            model TEXT NOT NULL,
            role TEXT NOT NULL,
            risk_class TEXT NOT NULL,
            sample_tasks INTEGER NOT NULL DEFAULT 0,
            accepted_tasks INTEGER NOT NULL DEFAULT 0,
            total_attempts INTEGER NOT NULL DEFAULT 0,
            regression_count INTEGER NOT NULL DEFAULT 0,
            avg_retries REAL NOT NULL DEFAULT 0.0,
            avg_turnaround_min REAL NOT NULL DEFAULT 0.0,
            total_cost_usd_est REAL NOT NULL DEFAULT 0.0,
            cost_per_accepted_usd_est REAL NOT NULL DEFAULT 0.0,
            benchmark_graduated INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (engine_id, model, role, risk_class)
        );

        CREATE TABLE IF NOT EXISTS evidence_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            contract_sha256 TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            build_id TEXT NOT NULL DEFAULT '',
            duration_seconds REAL NOT NULL DEFAULT 0.0,
            artifact_paths_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            verified INTEGER NOT NULL DEFAULT 0,
            rejection_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS release_verifications (
            tag TEXT PRIMARY KEY,
            task_id TEXT,
            repo TEXT NOT NULL,
            merge_sha TEXT NOT NULL,
            previous_good_tag TEXT,
            artifact_sha256 TEXT NOT NULL,
            smoke_command TEXT NOT NULL,
            smoke_status TEXT NOT NULL,
            smoke_detail TEXT NOT NULL DEFAULT '',
            verified_at TEXT,
            rolled_back_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_phase_id ON tasks(phase_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_task_contracts_task ON task_contracts(task_id, version);
        CREATE INDEX IF NOT EXISTS idx_task_attempts_task ON task_attempts(task_id, id);
        CREATE INDEX IF NOT EXISTS idx_task_transitions_task ON task_transitions(task_id, id);
        CREATE INDEX IF NOT EXISTS idx_remote_ops_task ON remote_operations(task_id, op_kind);
        CREATE INDEX IF NOT EXISTS idx_pipeline_ledger_task ON pipeline_ledger(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_evidence_records_task ON evidence_records(task_id, commit_sha);
        """
    )


MIGRATIONS: list[tuple[int, str, MigrationFn]] = [
    (1, "001_initial_schema", _migration_001_initial_schema),
    (2, "002_project_columns", _migration_002_project_columns),
    (3, "003_nullable_router_step", _migration_003_nullable_router_step),
    (4, "004_token_optimizer_tables", _migration_004_token_optimizer_tables),
    (5, "005_agent_pipeline", _migration_005_agent_pipeline),
]


class Store:
    """Durable SQLite store using WAL mode and ordered schema migrations."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def migrate(self) -> list[int]:
        """Apply all pending migrations in order and return applied version numbers."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                """
            )
        rows = self._conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        applied_versions = {int(row["version"]) for row in rows}
        newly_applied: list[int] = []
        for version, name, fn in MIGRATIONS:
            if version in applied_versions:
                continue
            with self._conn:
                fn(self._conn)
                self._conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, _utc_now()),
                )
            newly_applied.append(version)
        return newly_applied

    def current_schema_version(self) -> int:
        """Return the highest applied migration version, or 0 if uninitialized."""
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return 0
        ver = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(ver["v"]) if ver and ver["v"] is not None else 0

    def list_tables(self) -> set[str]:
        """Return all user table names present in the database."""
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {str(row["name"]) for row in rows}

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def create_project(
        self,
        project_id: str,
        name: str,
        repo: str,
        local_path: str,
        *,
        build_rules: str = "",
        ui_contract: str = "",
        release_workflow: str = "Android build",
        artifact_glob: str = "",
        artifact_prefix: str = "",
        tag_prefix: str = "v1.0.0-preview.",
        output_folder: str = "",
        output_dir: str = "",
        critical_paths_json: str = "[]",
    ) -> dict[str, Any]:
        now = _utc_now()
        effective_output = output_dir or output_folder
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO projects (
                    id, name, repo, local_path, build_rules, ui_contract,
                    release_workflow, artifact_glob, artifact_prefix, tag_prefix,
                    output_folder, output_dir, critical_paths_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    name,
                    repo,
                    local_path,
                    build_rules,
                    ui_contract,
                    release_workflow,
                    artifact_glob,
                    artifact_prefix,
                    tag_prefix,
                    effective_output,
                    effective_output,
                    critical_paths_json,
                    now,
                    now,
                ),
            )
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_project(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        if not kwargs:
            proj = self.get_project(project_id)
            if proj is None:
                raise KeyError(f"Project not found: {project_id}")
            return proj
        kwargs["updated_at"] = _utc_now()
        if "output_dir" in kwargs and "output_folder" not in kwargs:
            kwargs["output_folder"] = kwargs["output_dir"]
        elif "output_folder" in kwargs and "output_dir" not in kwargs:
            kwargs["output_dir"] = kwargs["output_folder"]
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [project_id]
        with self._conn:
            self._conn.execute(f"UPDATE projects SET {fields} WHERE id = ?", values)
        return dict(self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def delete_project(self, project_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def create_plan(self, plan_id: str, project_id: str, title: str, goal: str = "") -> dict[str, Any]:
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO plans (id, project_id, title, goal, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'draft', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    goal = excluded.goal,
                    updated_at = excluded.updated_at
                """,
                (plan_id, project_id, title, goal, now, now),
            )
        row = self._conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return dict(row)

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_plans(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            rows = self._conn.execute("SELECT * FROM plans WHERE project_id = ? ORDER BY created_at ASC", (project_id,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM plans ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def delete_plan(self, plan_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))

    def upsert_phase(
        self,
        phase_id: str,
        plan_id: str,
        title: str,
        branch: str,
        *,
        status: str = "pending",
        attempt: int = 0,
        pr_number: int | None = None,
        prompt: str = "",
        depends_on_json: str = "[]",
        size: str = "medium",
        position: int = 0,
        ladder: str = "default",
        timeout_minutes: int = 180,
        auto_merge: bool = True,
        release_after_merge: bool = False,
        force_rerun: bool = False,
    ) -> dict[str, Any]:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO phases (
                    id, plan_id, title, prompt, depends_on_json, size, position,
                    status, attempt, pr_number, branch, ladder, timeout_minutes, auto_merge, release_after_merge, force_rerun
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    prompt = excluded.prompt,
                    depends_on_json = excluded.depends_on_json,
                    size = excluded.size,
                    position = excluded.position,
                    status = excluded.status,
                    attempt = excluded.attempt,
                    pr_number = excluded.pr_number,
                    branch = excluded.branch,
                    ladder = excluded.ladder, timeout_minutes = excluded.timeout_minutes,
                    auto_merge = excluded.auto_merge, release_after_merge = excluded.release_after_merge,
                    force_rerun = excluded.force_rerun
                """,
                (
                    phase_id,
                    plan_id,
                    title,
                    prompt,
                    depends_on_json,
                    size,
                    position,
                    status,
                    attempt,
                    pr_number,
                    branch, ladder, timeout_minutes, int(auto_merge), int(release_after_merge), int(force_rerun),
                ),
            )
        row = self._conn.execute("SELECT * FROM phases WHERE id = ?", (phase_id,)).fetchone()
        return dict(row)

    def get_phase(self, phase_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM phases WHERE id = ?", (phase_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_phases(self, plan_id: str | None = None) -> list[dict[str, Any]]:
        if plan_id:
            rows = self._conn.execute(
                "SELECT * FROM phases WHERE plan_id = ? ORDER BY position ASC, id ASC", (plan_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM phases ORDER BY position ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_phase(self, phase_id: str, **kwargs: Any) -> dict[str, Any]:
        if not kwargs:
            p = self.get_phase(phase_id)
            if p is None:
                raise KeyError(f"Phase not found: {phase_id}")
            return p
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [phase_id]
        with self._conn:
            self._conn.execute(f"UPDATE phases SET {fields} WHERE id = ?", values)
        row = self._conn.execute("SELECT * FROM phases WHERE id = ?", (phase_id,)).fetchone()
        return dict(row)

    def delete_phase(self, phase_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM phases WHERE id = ?", (phase_id,))

    def record_step(
        self,
        phase_id: str,
        kind: str,
        runner: str,
        *,
        rc: int | None = None,
        verified: int = 0,
        detail: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> int:
        now = _utc_now()
        start = started_at or now
        fin = finished_at or now
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO steps (phase_id, kind, runner, rc, verified, detail, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (phase_id, kind, runner, rc, verified, detail, start, fin),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def update_step(self, step_id: int, **kwargs: Any) -> None:
        if not kwargs:
            return
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [step_id]
        with self._conn:
            self._conn.execute(f"UPDATE steps SET {fields} WHERE id = ?", values)

    def get_step(self, step_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_steps(self, phase_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE phase_id = ? ORDER BY id ASC", (phase_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_guard_result(
        self,
        phase_id: str,
        guard: str,
        passed: bool,
        detail: str = "",
    ) -> int:
        now = _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO guard_results (phase_id, guard, passed, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (phase_id, guard, 1 if passed else 0, detail, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_guard_results(self, phase_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM guard_results WHERE phase_id = ? ORDER BY id ASC", (phase_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_handoff(
        self,
        phase_id: str,
        from_engine: str,
        to_engine: str,
        reason: str,
        checkpoint_sha: str,
        ts: str | None = None,
    ) -> int:
        timestamp = ts or _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO handoffs (phase_id, from_engine, to_engine, reason, checkpoint_sha, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (phase_id, from_engine, to_engine, reason, checkpoint_sha, timestamp),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_handoffs(self, phase_id: str | None = None) -> list[dict[str, Any]]:
        if phase_id:
            rows = self._conn.execute(
                "SELECT * FROM handoffs WHERE phase_id = ? ORDER BY id ASC", (phase_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM handoffs ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def park_phase(
        self,
        phase_id: str,
        reason: str,
        next_step: str,
        stacked_children: list[str] | None = None,
    ) -> None:
        now = _utc_now()
        stacked_json = json.dumps(stacked_children or [])
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO parked (phase_id, reason, next_step, stacked_children_json, parked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(phase_id) DO UPDATE SET
                    reason = excluded.reason,
                    next_step = excluded.next_step,
                    stacked_children_json = excluded.stacked_children_json,
                    parked_at = excluded.parked_at
                """,
                (phase_id, reason, next_step, stacked_json, now),
            )
            self._conn.execute("UPDATE phases SET status = 'parked' WHERE id = ?", (phase_id,))

    def unpark_phase(self, phase_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM parked WHERE phase_id = ?", (phase_id,))

    def get_parked(self, phase_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM parked WHERE phase_id = ?", (phase_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_parked(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM parked ORDER BY parked_at ASC").fetchall()
        return [dict(r) for r in rows]

    def record_login_check(
        self,
        tool: str,
        state: str,
        detail: str = "",
        back_at: str | None = None,
    ) -> int:
        now = _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO login_checks (tool, state, detail, back_at, checked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tool, state, detail, back_at, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def get_latest_login_checks(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT lc.* FROM login_checks lc
            INNER JOIN (
                SELECT tool, MAX(id) AS max_id FROM login_checks GROUP BY tool
            ) latest ON lc.id = latest.max_id
            """
        ).fetchall()
        return {str(r["tool"]): dict(r) for r in rows}

    def record_ledger_entry(
        self,
        runner: str,
        model: str,
        event: str,
        reset_at: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> int:
        now = _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO runner_ledger (runner, model, event, reset_at, tokens_in, tokens_out, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (runner, model, event, reset_at, tokens_in, tokens_out, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def get_ledger_entries(self, runner: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if runner:
            rows = self._conn.execute(
                "SELECT * FROM runner_ledger WHERE runner = ? ORDER BY id DESC LIMIT ?",
                (runner, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runner_ledger ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_cooldown(self, runner: str, model: str = "") -> str | None:
        now = _utc_now()
        if model:
            row = self._conn.execute(
                """
                SELECT reset_at FROM runner_ledger
                WHERE runner = ? AND (model = ? OR model = 'default' OR model = '')
                  AND reset_at IS NOT NULL AND reset_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (runner, model, now),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT reset_at FROM runner_ledger
                WHERE runner = ? AND reset_at IS NOT NULL AND reset_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (runner, now),
            ).fetchone()
        return str(row["reset_at"]) if row is not None else None

    def record_duration(self, kind: str, runner: str, seconds: float) -> int:
        now = _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO durations (kind, runner, seconds, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (kind, runner, float(seconds), now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def get_durations(self, kind: str | None = None, runner: str | None = None) -> list[float]:
        if kind and runner:
            rows = self._conn.execute(
                "SELECT seconds FROM durations WHERE kind = ? AND runner = ? ORDER BY id ASC",
                (kind, runner),
            ).fetchall()
        elif kind:
            rows = self._conn.execute(
                "SELECT seconds FROM durations WHERE kind = ? ORDER BY id ASC", (kind,)
            ).fetchall()
        elif runner:
            rows = self._conn.execute(
                "SELECT seconds FROM durations WHERE runner = ? ORDER BY id ASC", (runner,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT seconds FROM durations ORDER BY id ASC").fetchall()
        return [float(r["seconds"]) for r in rows]

    def upsert_engine(
        self,
        engine_id: str,
        runner: str,
        model: str,
        *,
        effort: str = "medium",
        skills_json: str = "{}",
        cost_tier: str = "standard",
        budget_json: str = "{}",
        enabled: int = 1,
        pinned: int = 0,
        command_template: str = "[]",
        usage_parser: str = "",
        quota_patterns_json: str = "[]",
    ) -> dict[str, Any]:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO engines (
                    id, runner, model, effort, skills_json, cost_tier,
                    budget_json, enabled, pinned, command_template, usage_parser, quota_patterns_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    runner = excluded.runner,
                    model = excluded.model,
                    effort = excluded.effort,
                    skills_json = excluded.skills_json,
                    cost_tier = excluded.cost_tier,
                    budget_json = excluded.budget_json,
                    enabled = excluded.enabled,
                    pinned = excluded.pinned,
                    command_template = excluded.command_template,
                    usage_parser = excluded.usage_parser,
                    quota_patterns_json = excluded.quota_patterns_json
                """,
                (
                    engine_id, runner, model, effort, skills_json, cost_tier,
                    budget_json, enabled, pinned, command_template, usage_parser, quota_patterns_json,
                ),
            )
        row = self._conn.execute("SELECT * FROM engines WHERE id = ?", (engine_id,)).fetchone()
        return dict(row)

    def get_engine(self, engine_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM engines WHERE id = ?", (engine_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_engines(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM engines ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def set_priority_list(self, items: list[dict[str, Any]]) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM priority_list")
            for idx, item in enumerate(items, start=1):
                self._conn.execute(
                    """
                    INSERT INTO priority_list (position, engine, model, effort)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        item.get("position", idx),
                        item["engine"],
                        item.get("model", ""),
                        item.get("effort", "medium"),
                    ),
                )

    def get_priority_list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM priority_list ORDER BY position ASC").fetchall()
        return [dict(r) for r in rows]

    def upsert_phase_assignment(
        self,
        phase_id: str,
        *,
        planned_engine: str | None = None,
        override_engine: str | None = None,
        model: str | None = None,
        effort: str = "medium",
    ) -> dict[str, Any]:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO phase_assignments (phase_id, planned_engine, override_engine, model, effort)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(phase_id) DO UPDATE SET
                    planned_engine = excluded.planned_engine,
                    override_engine = excluded.override_engine,
                    model = excluded.model,
                    effort = excluded.effort
                """,
                (phase_id, planned_engine, override_engine, model, effort),
            )
        row = self._conn.execute("SELECT * FROM phase_assignments WHERE phase_id = ?", (phase_id,)).fetchone()
        return dict(row)

    def get_phase_assignment(self, phase_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM phase_assignments WHERE phase_id = ?", (phase_id,)).fetchone()
        return dict(row) if row is not None else None

    def record_router_decision(
        self,
        chosen: str,
        reason: str,
        step_id: int | None = None,
        scores_json: str = "{}",
    ) -> int:
        now = _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO router_decisions (step_id, chosen, reason, scores_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (step_id, chosen, reason, scores_json, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_router_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM router_decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_council_run(
        self,
        plan_id: str,
        role: str,
        engine: str,
        input_text: str = "",
        output_text: str = "",
        usage_estimate: dict[str, Any] | str = "{}",
    ) -> int:
        now = _utc_now()
        usage_json = json.dumps(usage_estimate) if isinstance(usage_estimate, dict) else str(usage_estimate)
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO council_runs (plan_id, role, engine, input, output, usage_estimate, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (plan_id, role, engine, input_text, output_text, usage_json, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_council_runs(self, plan_id: str | None = None) -> list[dict[str, Any]]:
        if plan_id:
            rows = self._conn.execute(
                "SELECT * FROM council_runs WHERE plan_id = ? ORDER BY id ASC", (plan_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM council_runs ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def update_engine_stats(
        self,
        engine: str,
        size: str,
        success: bool,
        minutes: float,
        est_cost: float = 0.0,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO engine_stats (engine, size, attempts, successes, minutes, est_cost)
                VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(engine, size) DO UPDATE SET
                    attempts = attempts + 1,
                    successes = successes + excluded.successes,
                    minutes = minutes + excluded.minutes,
                    est_cost = est_cost + excluded.est_cost
                """,
                (engine, size, 1 if success else 0, minutes, est_cost),
            )

    def get_engine_stats(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM engine_stats ORDER BY engine, size").fetchall()
        return [dict(r) for r in rows]

    def append_event(
        self,
        *,
        level: str,
        kind: str,
        plain_text: str,
        phase_id: str | None = None,
        raw_path: str | None = None,
        ts: str | None = None,
    ) -> int:
        """Append an immutable SHA-256 hash-chained event row and return its ID."""
        timestamp = ts or _utc_now()
        with self._conn:
            last = self._conn.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = str(last["hash"]) if last is not None else GENESIS_HASH
            digest = _compute_event_hash(prev_hash, timestamp, level, phase_id, kind, plain_text, raw_path)
            cur = self._conn.execute(
                """
                INSERT INTO events (ts, level, phase_id, kind, plain_text, raw_path, prev_hash, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp, level, phase_id, kind, plain_text, raw_path, prev_hash, digest),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM events ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def verify_event_chain(self) -> bool:
        """Verify that every event hash matches the previous event in sequence."""
        rows = self._conn.execute("SELECT * FROM events ORDER BY id ASC").fetchall()
        expected_prev = GENESIS_HASH
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return False
            expected_hash = _compute_event_hash(
                row["prev_hash"],
                row["ts"],
                row["level"],
                row["phase_id"],
                row["kind"],
                row["plain_text"],
                row["raw_path"],
            )
            if row["hash"] != expected_hash:
                return False
            expected_prev = row["hash"]
        return True

    def upsert_token_tool_state(
        self,
        tool: str,
        *,
        enabled: bool | None = None,
        installed_version: str | None = None,
        sha256_verified: bool | None = None,
        isolation_passed: bool | None = None,
        isolation_method: str | None = None,
        auto_disabled: bool | None = None,
        warning: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_token_tool_state(tool) or {
            "tool": tool,
            "enabled": 0,
            "installed_version": "",
            "sha256_verified": 0,
            "isolation_passed": 0,
            "isolation_method": "",
            "auto_disabled": 0,
            "warning": "",
        }
        now = _utc_now()
        next_enabled = int(enabled) if enabled is not None else int(existing["enabled"])
        next_ver = installed_version if installed_version is not None else str(existing["installed_version"])
        next_sha = int(sha256_verified) if sha256_verified is not None else int(existing["sha256_verified"])
        next_iso = int(isolation_passed) if isolation_passed is not None else int(existing["isolation_passed"])
        next_iso_m = isolation_method if isolation_method is not None else str(existing["isolation_method"])
        next_auto = int(auto_disabled) if auto_disabled is not None else int(existing["auto_disabled"])
        next_warn = warning if warning is not None else str(existing["warning"])
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO token_tool_states (
                    tool, enabled, installed_version, sha256_verified,
                    isolation_passed, isolation_method, auto_disabled, warning, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool) DO UPDATE SET
                    enabled = excluded.enabled,
                    installed_version = excluded.installed_version,
                    sha256_verified = excluded.sha256_verified,
                    isolation_passed = excluded.isolation_passed,
                    isolation_method = excluded.isolation_method,
                    auto_disabled = excluded.auto_disabled,
                    warning = excluded.warning,
                    updated_at = excluded.updated_at
                """,
                (tool, next_enabled, next_ver, next_sha, next_iso, next_iso_m, next_auto, next_warn, now),
            )
        row = self._conn.execute("SELECT * FROM token_tool_states WHERE tool = ?", (tool,)).fetchone()
        return dict(row)

    def get_token_tool_state(self, tool: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM token_tool_states WHERE tool = ?", (tool,)).fetchone()
        return dict(row) if row is not None else None

    def list_token_tool_states(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM token_tool_states ORDER BY tool ASC").fetchall()
        return [dict(r) for r in rows]

    def record_token_ab_run(
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
        ts = created_at or _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO token_ab_runs (
                    tool, enabled, phase_kind, engine, tokens_in, tokens_out,
                    turns, retry_turns, success, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool,
                    1 if enabled else 0,
                    phase_kind,
                    engine,
                    int(tokens_in),
                    int(tokens_out),
                    int(turns),
                    int(retry_turns),
                    1 if success else 0,
                    ts,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_token_ab_runs(
        self,
        *,
        tool: str | None = None,
        phase_kind: str | None = None,
        since_iso: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if tool:
            clauses.append("tool = ?")
            params.append(tool)
        if phase_kind:
            clauses.append("phase_kind = ?")
            params.append(phase_kind)
        if since_iso:
            clauses.append("created_at >= ?")
            params.append(since_iso)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM token_ab_runs {where} ORDER BY id ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Agent Pipeline persistence (migration 005_agent_pipeline)
    # ------------------------------------------------------------------

    def create_task_with_contract(
        self,
        *,
        task_id: str,
        phase_id: str,
        plan_id: str,
        title: str,
        branch: str,
        risk_class: str,
        risk_factors: dict[str, Any],
        acceptance_criteria: list[dict[str, Any]],
        allowed_globs: list[str],
        forbidden_globs: list[str],
        budget: dict[str, Any],
        owner_approval_required: bool = False,
        owner_override_engine: str | None = None,
        contract_id: str | None = None,
        frozen_at: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically create a task in QUEUED state and freeze version 1 of its contract."""
        from nightshift.engine.pipeline_contracts import canonical_contract_json, compute_contract_sha256

        now = frozen_at or _utc_now()
        cid = contract_id or f"{task_id}-v1"
        payload = {
            "task_id": task_id,
            "version": 1,
            "phase_id": phase_id,
            "title": title,
            "risk_class": risk_class,
            "risk_factors": risk_factors,
            "acceptance_criteria": acceptance_criteria,
            "allowed_globs": allowed_globs,
            "forbidden_globs": forbidden_globs,
            "budget": budget,
            "owner_approval_required": bool(owner_approval_required),
            "frozen_at": now,
        }
        canonical = canonical_contract_json(payload)
        digest = compute_contract_sha256(canonical)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    id, phase_id, plan_id, title, branch, risk_class, status,
                    contract_id, contract_sha256, owner_override_engine,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    phase_id,
                    plan_id,
                    title,
                    branch,
                    risk_class,
                    cid,
                    digest,
                    owner_override_engine,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO task_contracts (
                    id, task_id, version, risk_class, risk_factors_json,
                    acceptance_criteria_json, allowed_globs_json, forbidden_globs_json,
                    budget_json, owner_approval_required, canonical_json, contract_sha256, frozen_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    task_id,
                    risk_class,
                    json.dumps(risk_factors, sort_keys=True),
                    json.dumps(acceptance_criteria, sort_keys=True),
                    json.dumps(allowed_globs),
                    json.dumps(forbidden_globs),
                    json.dumps(budget, sort_keys=True),
                    1 if owner_approval_required else 0,
                    canonical,
                    digest,
                    now,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        task = self.get_task(task_id)
        contract = self.get_task_contract(cid)
        assert task is not None and contract is not None
        return task, contract

    def revise_task_contract(
        self,
        *,
        task_id: str,
        risk_class: str,
        risk_factors: dict[str, Any],
        acceptance_criteria: list[dict[str, Any]],
        allowed_globs: list[str],
        forbidden_globs: list[str],
        budget: dict[str, Any],
        owner_approval_required: bool = False,
        actor: str = "owner",
        reason: str = "Contract revised",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Append version + 1 to task_contracts and invalidate prior evidence, review, and approvals."""
        from nightshift.engine.pipeline_contracts import canonical_contract_json, compute_contract_sha256

        now = _utc_now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            task_row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task_row is None:
                raise KeyError(f"Unknown task: {task_id}")
            ver_row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM task_contracts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            next_version = int(ver_row["v"]) + 1
            cid = f"{task_id}-v{next_version}"
            payload = {
                "task_id": task_id,
                "version": next_version,
                "phase_id": str(task_row["phase_id"]),
                "title": str(task_row["title"]),
                "risk_class": risk_class,
                "risk_factors": risk_factors,
                "acceptance_criteria": acceptance_criteria,
                "allowed_globs": allowed_globs,
                "forbidden_globs": forbidden_globs,
                "budget": budget,
                "owner_approval_required": bool(owner_approval_required),
                "frozen_at": now,
            }
            canonical = canonical_contract_json(payload)
            digest = compute_contract_sha256(canonical)
            self._conn.execute(
                """
                INSERT INTO task_contracts (
                    id, task_id, version, risk_class, risk_factors_json,
                    acceptance_criteria_json, allowed_globs_json, forbidden_globs_json,
                    budget_json, owner_approval_required, canonical_json, contract_sha256, frozen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    task_id,
                    next_version,
                    risk_class,
                    json.dumps(risk_factors, sort_keys=True),
                    json.dumps(acceptance_criteria, sort_keys=True),
                    json.dumps(allowed_globs),
                    json.dumps(forbidden_globs),
                    json.dumps(budget, sort_keys=True),
                    1 if owner_approval_required else 0,
                    canonical,
                    digest,
                    now,
                ),
            )
            # Invalidate prior evidence records, review_sha, approved_sha, and owner approvals
            self._conn.execute(
                """
                UPDATE evidence_records
                SET verified = 0, rejection_reason = 'invalidated_by_contract_revision'
                WHERE task_id = ?
                """,
                (task_id,),
            )
            prev_status = str(task_row["status"])
            self._conn.execute(
                """
                UPDATE tasks SET
                    risk_class = ?,
                    contract_id = ?,
                    contract_sha256 = ?,
                    status = 'QUEUED',
                    blocked_from_status = '',
                    block_reason = '',
                    block_detail = '',
                    next_step = '',
                    review_sha = NULL,
                    approved_sha = NULL,
                    owner_approved_by = NULL,
                    owner_approved_sha = NULL,
                    owner_approved_at = NULL,
                    assertion_waiver_sha = NULL,
                    assertion_waiver_reason = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (risk_class, cid, digest, now, task_id),
            )
            self._conn.execute(
                """
                INSERT INTO task_transitions (
                    task_id, from_status, to_status, idempotency_key, reason, actor, created_at
                ) VALUES (?, ?, 'QUEUED', ?, ?, ?, ?)
                """,
                (task_id, prev_status, f"contract-rev:{task_id}:v{next_version}", reason, actor, now),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        task = self.get_task(task_id)
        contract = self.get_task_contract(cid)
        assert task is not None and contract is not None
        return task, contract

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_tasks(self, *, plan_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if plan_id:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(f"SELECT * FROM tasks {where} ORDER BY created_at DESC", params).fetchall()
        return [dict(r) for r in rows]

    def get_task_contract(self, contract_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM task_contracts WHERE id = ?", (contract_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_task_contracts(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_contracts WHERE task_id = ? ORDER BY version ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_task_fields(self, task_id: str, **fields: Any) -> dict[str, Any]:
        if not fields:
            task = self.get_task(task_id)
            if task is None:
                raise KeyError(f"Unknown task: {task_id}")
            return task
        fields["updated_at"] = _utc_now()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [task_id]
        with self._conn:
            self._conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", params)
        task = self.get_task(task_id)
        assert task is not None
        return task

    def record_task_attempt(
        self,
        *,
        task_id: str,
        stage: str,
        role: str,
        engine_id: str,
        provider: str,
        model: str = "",
        input_ref: str = "",
        output_ref: str = "",
        decision_reason: str = "",
        head_sha_before: str | None = None,
        head_sha_after: str | None = None,
        rc: int | None = None,
        outcome: str = "ok",
        wall_seconds: float = 0.0,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> int:
        now = _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO task_attempts (
                    task_id, stage, role, engine_id, provider, model,
                    input_ref, output_ref, decision_reason,
                    head_sha_before, head_sha_after, rc, outcome,
                    wall_seconds, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    stage,
                    role,
                    engine_id,
                    provider,
                    model,
                    input_ref,
                    output_ref,
                    decision_reason,
                    head_sha_before,
                    head_sha_after,
                    rc,
                    outcome,
                    float(wall_seconds),
                    started_at or now,
                    finished_at or now,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_task_attempts(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_task_transitions(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_transitions WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_evidence(
        self,
        *,
        task_id: str,
        contract_sha256: str,
        commit_sha: str,
        kind: str,
        source: str,
        build_id: str = "",
        duration_seconds: float = 0.0,
        artifact_paths: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        verified: bool = False,
        rejection_reason: str = "",
    ) -> int:
        now = _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO evidence_records (
                    task_id, contract_sha256, commit_sha, kind, source, build_id,
                    duration_seconds, artifact_paths_json, metadata_json,
                    verified, rejection_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    contract_sha256,
                    commit_sha,
                    kind,
                    source,
                    build_id,
                    float(duration_seconds),
                    json.dumps(artifact_paths or []),
                    json.dumps(metadata or {}, sort_keys=True),
                    1 if verified else 0,
                    rejection_reason,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_evidence_records(
        self,
        task_id: str,
        *,
        commit_sha: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["task_id = ?"]
        params: list[Any] = [task_id]
        if commit_sha:
            clauses.append("commit_sha = ?")
            params.append(commit_sha)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        rows = self._conn.execute(
            f"SELECT * FROM evidence_records WHERE {' AND '.join(clauses)} ORDER BY id ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def record_pipeline_ledger(
        self,
        *,
        task_id: str,
        role: str,
        engine_id: str,
        provider: str,
        model: str,
        risk_class: str,
        outcome: str,
        attempt_id: int | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        est_token_cost_usd: float = 0.0,
        ci_minutes: float = 0.0,
        artifact_mb: float = 0.0,
        wall_seconds: float = 0.0,
        is_estimate: bool = True,
        created_at: str | None = None,
    ) -> int:
        ts = created_at or _utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO pipeline_ledger (
                    task_id, attempt_id, role, engine_id, provider, model, risk_class,
                    tokens_in, tokens_out, est_token_cost_usd, ci_minutes, artifact_mb,
                    wall_seconds, outcome, is_estimate, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempt_id,
                    role,
                    engine_id,
                    provider,
                    model,
                    risk_class,
                    int(tokens_in),
                    int(tokens_out),
                    float(est_token_cost_usd),
                    float(ci_minutes),
                    float(artifact_mb),
                    float(wall_seconds),
                    outcome,
                    1 if is_estimate else 0,
                    ts,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_pipeline_ledger(
        self,
        *,
        task_id: str | None = None,
        since_iso: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if since_iso:
            clauses.append("created_at >= ?")
            params.append(since_iso)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM pipeline_ledger {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_model_score(
        self,
        *,
        engine_id: str,
        model: str,
        role: str,
        risk_class: str,
        sample_tasks: int,
        accepted_tasks: int,
        total_attempts: int,
        regression_count: int,
        avg_retries: float,
        avg_turnaround_min: float,
        total_cost_usd_est: float,
        cost_per_accepted_usd_est: float,
        benchmark_graduated: bool,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO model_scores (
                    engine_id, model, role, risk_class, sample_tasks, accepted_tasks,
                    total_attempts, regression_count, avg_retries, avg_turnaround_min,
                    total_cost_usd_est, cost_per_accepted_usd_est, benchmark_graduated, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(engine_id, model, role, risk_class) DO UPDATE SET
                    sample_tasks = excluded.sample_tasks,
                    accepted_tasks = excluded.accepted_tasks,
                    total_attempts = excluded.total_attempts,
                    regression_count = excluded.regression_count,
                    avg_retries = excluded.avg_retries,
                    avg_turnaround_min = excluded.avg_turnaround_min,
                    total_cost_usd_est = excluded.total_cost_usd_est,
                    cost_per_accepted_usd_est = excluded.cost_per_accepted_usd_est,
                    benchmark_graduated = excluded.benchmark_graduated,
                    updated_at = excluded.updated_at
                """,
                (
                    engine_id,
                    model,
                    role,
                    risk_class,
                    int(sample_tasks),
                    int(accepted_tasks),
                    int(total_attempts),
                    int(regression_count),
                    float(avg_retries),
                    float(avg_turnaround_min),
                    float(total_cost_usd_est),
                    float(cost_per_accepted_usd_est),
                    1 if benchmark_graduated else 0,
                    now,
                ),
            )
        row = self._conn.execute(
            "SELECT * FROM model_scores WHERE engine_id = ? AND model = ? AND role = ? AND risk_class = ?",
            (engine_id, model, role, risk_class),
        ).fetchone()
        assert row is not None
        return dict(row)

    def list_model_scores(
        self,
        *,
        role: str | None = None,
        risk_class: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if role:
            clauses.append("role = ?")
            params.append(role)
        if risk_class:
            clauses.append("risk_class = ?")
            params.append(risk_class)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM model_scores {where} ORDER BY cost_per_accepted_usd_est ASC, engine_id ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_release_verification(
        self,
        *,
        tag: str,
        repo: str,
        merge_sha: str,
        artifact_sha256: str,
        smoke_command: str,
        smoke_status: str,
        task_id: str | None = None,
        previous_good_tag: str | None = None,
        smoke_detail: str = "",
        verified_at: str | None = None,
        rolled_back_at: str | None = None,
    ) -> dict[str, Any]:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO release_verifications (
                    tag, task_id, repo, merge_sha, previous_good_tag,
                    artifact_sha256, smoke_command, smoke_status, smoke_detail,
                    verified_at, rolled_back_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tag) DO UPDATE SET
                    smoke_status = excluded.smoke_status,
                    smoke_detail = excluded.smoke_detail,
                    verified_at = COALESCE(excluded.verified_at, release_verifications.verified_at),
                    rolled_back_at = COALESCE(excluded.rolled_back_at, release_verifications.rolled_back_at)
                """,
                (
                    tag,
                    task_id,
                    repo,
                    merge_sha,
                    previous_good_tag,
                    artifact_sha256,
                    smoke_command,
                    smoke_status,
                    smoke_detail,
                    verified_at,
                    rolled_back_at,
                ),
            )
        row = self._conn.execute("SELECT * FROM release_verifications WHERE tag = ?", (tag,)).fetchone()
        assert row is not None
        return dict(row)

    def list_release_verifications(self, *, repo: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if repo:
            rows = self._conn.execute(
                "SELECT * FROM release_verifications WHERE repo = ? ORDER BY rowid DESC LIMIT ?",
                (repo, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM release_verifications ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_release_verification(self, tag: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM release_verifications WHERE tag = ?", (tag,)).fetchone()
        return dict(row) if row is not None else None


