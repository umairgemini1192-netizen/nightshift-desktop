"""SQLite persistence layer with WAL mode, migrations, and hash-chained audit events.

Schema implements BLUEPRINT section 6 in full so every engine step, router
decision, handoff, login check, guard result, and event is durable across restarts.
"""

from __future__ import annotations

import hashlib
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
            artifact_glob TEXT NOT NULL DEFAULT '',
            output_folder TEXT NOT NULL DEFAULT '',
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


MIGRATIONS: list[tuple[int, str, MigrationFn]] = [
    (1, "001_initial_schema", _migration_001_initial_schema),
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
        artifact_glob: str = "",
        output_folder: str = "",
        critical_paths_json: str = "[]",
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO projects (
                    id, name, repo, local_path, build_rules, artifact_glob,
                    output_folder, critical_paths_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    name,
                    repo,
                    local_path,
                    build_rules,
                    artifact_glob,
                    output_folder,
                    critical_paths_json,
                    now,
                    now,
                ),
            )
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def create_plan(self, plan_id: str, project_id: str, title: str, goal: str = "") -> dict[str, Any]:
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO plans (id, project_id, title, goal, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'draft', ?, ?)
                """,
                (plan_id, project_id, title, goal, now, now),
            )
        row = self._conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return dict(row)

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
