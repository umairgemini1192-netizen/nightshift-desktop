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
            step_id INTEGER NOT NULL,
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


MIGRATIONS: list[tuple[int, str, MigrationFn]] = [
    (1, "001_initial_schema", _migration_001_initial_schema),
    (2, "002_project_columns", _migration_002_project_columns),
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
    ) -> dict[str, Any]:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO phases (
                    id, plan_id, title, prompt, depends_on_json, size, position,
                    status, attempt, pr_number, branch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    prompt = excluded.prompt,
                    depends_on_json = excluded.depends_on_json,
                    size = excluded.size,
                    position = excluded.position,
                    status = excluded.status,
                    attempt = excluded.attempt,
                    pr_number = excluded.pr_number,
                    branch = excluded.branch
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
                    branch,
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
                WHERE runner = ? AND model = ? AND reset_at IS NOT NULL AND reset_at > ?
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
        step_id: int,
        chosen: str,
        reason: str,
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
