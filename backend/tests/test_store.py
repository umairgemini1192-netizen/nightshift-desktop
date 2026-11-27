"""Tests for the SQLite store, migrations, WAL mode, and hash-chained events."""

from __future__ import annotations

from pathlib import Path

from nightshift.events import Event, EventBus
from nightshift.store import GENESIS_HASH, Store

EXPECTED_TABLES = {
    "schema_migrations",
    "projects",
    "plans",
    "phases",
    "steps",
    "engines",
    "router_decisions",
    "phase_assignments",
    "priority_list",
    "guard_results",
    "handoffs",
    "parked",
    "login_checks",
    "engine_stats",
    "council_runs",
    "runner_ledger",
    "durations",
    "settings",
    "events",
    "token_tool_states",
    "token_ab_runs",
    "tasks",
    "task_contracts",
    "task_attempts",
    "task_transitions",
    "remote_operations",
    "pipeline_ledger",
    "model_scores",
    "evidence_records",
    "release_verifications",
}


def test_store_migrations_create_full_schema_and_are_idempotent(tmp_path: Path) -> None:
    db_file = tmp_path / "nightshift.db"
    with Store(db_file) as store:
        assert store.current_schema_version() == 0
        applied_first = store.migrate()
        assert applied_first == [1, 2, 3, 4, 5, 6]
        assert store.current_schema_version() == 6
        assert EXPECTED_TABLES.issubset(store.list_tables())

        # Re-running migrate must be a safe no-op.
        applied_second = store.migrate()
        assert applied_second == []

        journal_mode = store.connection.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(journal_mode).lower() == "wal"


def test_store_settings_and_project_plan_phase_persistence(tmp_path: Path) -> None:
    db_file = tmp_path / "nightshift.db"
    with Store(db_file) as store:
        store.migrate()

        assert store.get_setting("theme", "system") == "system"
        store.set_setting("theme", "dark")
        assert store.get_setting("theme") == "dark"
        store.set_setting("theme", "light")
        assert store.get_setting("theme") == "light"

        proj = store.create_project(
            "proj-1",
            "Night-Shift Desktop",
            "owner/nightshift-desktop",
            "D:/repos/nightshift-desktop",
        )
        assert proj["id"] == "proj-1"
        assert len(store.list_projects()) == 1

        plan = store.create_plan("plan-1", "proj-1", "Foundation", "Build v1 desktop app")
        assert plan["status"] == "draft"

        phase = store.upsert_phase(
            "phase-1",
            "plan-1",
            "Shell scaffold",
            "feat/p1-shell-scaffold",
            status="running",
            attempt=1,
            pr_number=42,
        )
        assert phase["status"] == "running"
        assert phase["pr_number"] == 42

        updated = store.upsert_phase(
            "phase-1",
            "plan-1",
            "Shell scaffold",
            "feat/p1-shell-scaffold",
            status="merged",
            attempt=1,
            pr_number=42,
        )
        assert updated["status"] == "merged"


def test_store_hash_chained_events_and_tamper_detection(tmp_path: Path) -> None:
    db_file = tmp_path / "nightshift.db"
    with Store(db_file) as store:
        store.migrate()
        bus = EventBus(store=store)
        received: list[Event] = []
        unsubscribe = bus.subscribe("phase.started", received.append)

        first_id = bus.emit(Event(kind="phase.started", plain_text="Phase 1 started", phase_id="p1"))
        second_id = bus.emit(Event(kind="phase.done", plain_text="Phase 1 merged", phase_id="p1"))
        unsubscribe()

        assert first_id == 1
        assert second_id == 2
        assert len(received) == 1

        events = store.list_events()
        assert len(events) == 2
        assert events[0]["prev_hash"] == GENESIS_HASH
        assert events[1]["prev_hash"] == events[0]["hash"]
        assert store.verify_event_chain() is True

        # Tampering with an event must break verification.
        with store.connection:
            store.connection.execute(
                "UPDATE events SET plain_text = 'tampered' WHERE id = ?",
                (first_id,),
            )
        assert store.verify_event_chain() is False


def test_upgrade_existing_project_columns(tmp_path: Path) -> None:
    with Store(tmp_path / "old.db") as store:
        store.connection.executescript("""
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
            INSERT INTO schema_migrations VALUES (1, 'initial', 'now');
            CREATE TABLE projects (id TEXT PRIMARY KEY, output_folder TEXT NOT NULL DEFAULT '');
            INSERT INTO projects VALUES ('old', 'saved');
        """)
        assert store.migrate() == [2, 3, 4, 5, 6]
        row = store.connection.execute("SELECT * FROM projects").fetchone()
        assert row["output_dir"] == "saved"
        assert row["release_workflow"] == "Android build"
        assert row["required_ci_jobs_json"] is None
        assert row["required_artifacts_by_job_json"] == "{}"
        assert row["min_gate_duration_seconds"] == 5.0
        assert row["min_build_duration_seconds"] == 15.0
        assert row["screenshot_manifest_path"] == ""
        assert store.migrate() == []


def test_events_imports_in_fresh_interpreter() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "from nightshift.events import EventBus"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_migration_preserves_existing_router_decisions(tmp_path: Path) -> None:
    with Store(tmp_path / "old.db") as store:
        store.migrate()
        conn = store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            conn.execute("DROP TABLE router_decisions")
            conn.execute("""CREATE TABLE router_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, step_id INTEGER NOT NULL,
                chosen TEXT NOT NULL, reason TEXT NOT NULL, scores_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, FOREIGN KEY (step_id) REFERENCES steps(id) ON DELETE CASCADE)""")
            conn.execute("INSERT INTO router_decisions VALUES (7, 1, 'engine', 'reason', '{}', 'now')")
            conn.execute("DELETE FROM schema_migrations WHERE version = 3")
        assert store.migrate() == [3]
        assert conn.execute("SELECT chosen FROM router_decisions WHERE id = 7").fetchone()[0] == "engine"
        with conn:
            conn.execute("INSERT INTO router_decisions (step_id, chosen, reason, created_at) VALUES (NULL, 'other', 'reason', 'now')")
        assert conn.execute("SELECT COUNT(*) FROM router_decisions").fetchone()[0] == 2

