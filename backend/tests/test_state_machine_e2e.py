"""End-to-end tests for EngineStateMachine: multi-phase runs, crash recovery, Stop-after-phase, locks, and digests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig, SingleInstanceLock
from nightshift.engine.vcs import FakeGitGh
from nightshift.events import EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners import FakeRunner
from nightshift.store import Store


@pytest.fixture(autouse=True)
def isolate_host_preflight() -> Iterator[None]:
    """Use the fake command runner regardless of installed tools or disk space."""
    with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"), patch(
        "nightshift.engine.state_machine.shutil.disk_usage",
        return_value=(20 * 1024**3, 10 * 1024**3, 10 * 1024**3),
    ):
        yield


@pytest.mark.asyncio
async def test_full_multi_phase_execution_and_digest(tmp_path: Path) -> None:
    db = tmp_path / "e2e.db"
    output_dir = tmp_path / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)

    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()

        # Phase 1 creates PR, CI passes, merges, releases
        # Phase 2 depends on Phase 1, merges
        # Phase 3 already merged -> skipped
        def on_call(p: str, wd: Path, m: str | None, eff: str) -> None:
            # Runner creates PR in fake VCS
            if "p1" in str(wd):
                vcs.add_pr("feat/p1", number=201)
            elif "p2" in str(wd):
                vcs.add_pr("feat/p2", number=202)

        runners = {
            "agy": FakeRunner(name="agy", on_call=on_call),
            "gemini": FakeRunner.success("gemini"),
            "codex": FakeRunner.success("codex"),
            "claude": FakeRunner.success("claude"),
        }

        # Mock pre-flight disk usage and branch protection
        vcs.branch_protection["main"] = True
        vcs.merged_prs["feat/p3"] = {"number": 203, "url": "https://github.com/test/repo/pull/203"}

        project = ProjectConfig(
            id="proj_test",
            name="Test Desktop App",
            repo="owner/test-app",
            local_path=tmp_path / "repo",
            output_dir=output_dir,
            release_workflow="Desktop build",
            artifact_glob="*.zip",
        )
        project.local_path.mkdir(parents=True, exist_ok=True)

        phases = [
            PhaseDefinition(id="p1", title="Shell Scaffold", branch="feat/p1", prompt="Build shell", auto_merge=True, release_after_merge=True),
            PhaseDefinition(id="p2", title="Engine Core", branch="feat/p2", prompt="Build engine", depends_on=["p1"], auto_merge=True),
            PhaseDefinition(id="p3", title="Already Merged", branch="feat/p3", prompt="Already done"),
        ]

        mock_login_runner = lambda cmd, to: (0, "Logged in to github.com\nOK", "", False)
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=mock_login_runner)

        engine = EngineStateMachine(
            store=store,
            event_bus=events,
            quota_ledger=quota,
            vcs=vcs,
            runners=runners,
            login_checker=logins,
        )

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt_root"):
            results = await engine.execute_plan(project, phases, plan_id="plan_1")

        assert len(results) == 3
        # p1 completed and released
        assert results[0]["phase_id"] == "p1"
        assert results[0]["status"] == "COMPLETED"
        assert "Released" in results[0]["detail"]

        # p2 completed and merged
        assert results[1]["phase_id"] == "p2"
        assert results[1]["status"] == "COMPLETED"
        assert "Merged PR #202" in results[1]["detail"]

        # p3 skipped
        assert results[2]["phase_id"] == "p3"
        assert results[2]["status"] == "SKIPPED"

        # Check morning digest file produced
        digest_file = output_dir / "MORNING_DIGEST.md"
        assert digest_file.exists()
        digest_text = digest_file.read_text(encoding="utf-8")
        assert "Test Desktop App: Continuous Delivery Digest" in digest_text
        assert "Shell Scaffold" in digest_text
        assert "Engine Core" in digest_text


@pytest.mark.asyncio
async def test_stop_after_current_phase(tmp_path: Path) -> None:
    db = tmp_path / "stop.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()

        def on_call(p: str, wd: Path, m: str | None, eff: str) -> None:
            vcs.add_pr("feat/p1", number=301)
            # Simulate owner requesting stop while phase 1 is running
            engine.request_stop()

        runners = {"agy": FakeRunner(name="agy", on_call=on_call)}

        project = ProjectConfig(
            id="proj_stop",
            name="Stop App",
            repo="owner/stop-app",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "dist",
        )
        project.local_path.mkdir(parents=True, exist_ok=True)

        phases = [
            PhaseDefinition(id="p1", title="P1", branch="feat/p1", prompt="Work on P1"),
            PhaseDefinition(id="p2", title="P2", branch="feat/p2", prompt="Work on P2", depends_on=["p1"]),
        ]

        mock_login_runner = lambda cmd, to: (0, "Logged in to github.com\nOK", "", False)
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=mock_login_runner)

        engine = EngineStateMachine(
            store=store,
            event_bus=events,
            quota_ledger=quota,
            vcs=vcs,
            runners=runners,
            login_checker=logins,
        )

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt_root"):
            results = await engine.execute_plan(project, phases, plan_id="plan_stop")

        # P1 completed cleanly, P2 was NOT started!
        assert len(results) == 1
        assert results[0]["phase_id"] == "p1"
        assert results[0]["status"] == "COMPLETED"

        p2_record = store.get_phase("p2")
        assert p2_record is not None
        assert p2_record["status"] == "pending"  # Unstarted, ready to resume on next run!


@pytest.mark.asyncio
async def test_crash_recovery_resumes_cleanly(tmp_path: Path) -> None:
    """Test that restarting the engine after a crash resumes seamlessly from store state."""
    db = tmp_path / "resume.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()

        project = ProjectConfig(
            id="proj_resume",
            name="Resume App",
            repo="owner/resume-app",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "dist",
        )
        project.local_path.mkdir(parents=True, exist_ok=True)

        # Simulate prior run completed p1, and p2 had opened a PR before crashing
        store.create_project("proj_resume", "Resume App", "owner/resume-app", str(tmp_path / "repo"))
        store.create_plan("plan_r", "proj_resume", "Plan")
        store.upsert_phase("p1", "plan_r", "Phase 1", "feat/p1", status="merged")
        store.upsert_phase("p2", "plan_r", "Phase 2", "feat/p2", status="watching_ci", pr_number=402)

        vcs.merged_prs["feat/p1"] = {"number": 401, "url": "https://github.com/..."}
        vcs.add_pr("feat/p2", number=402)

        mock_login_runner = lambda cmd, to: (0, "Logged in to github.com\nOK", "", False)
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=mock_login_runner)

        runner = FakeRunner.success("agy")
        reviewer = FakeRunner.success("codex")
        engine = EngineStateMachine(
            store=store,
            event_bus=events,
            quota_ledger=quota,
            vcs=vcs,
            runners={"agy": runner, "codex": reviewer},
            login_checker=logins,
        )

        phases = [
            PhaseDefinition(id="p1", title="Phase 1", branch="feat/p1"),
            PhaseDefinition(id="p2", title="Phase 2", branch="feat/p2", depends_on=["p1"]),
        ]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt_root"):
            results = await engine.execute_plan(project, phases, plan_id="plan_r")

        assert len(results) == 2
        assert results[0]["status"] == "SKIPPED"
        assert results[1]["status"] == "COMPLETED"
        assert "Merged PR #402" in results[1]["detail"]

        # Runner was NOT called for implementation because it resumed at CI watch!
        assert len(runner.calls) == 0
        assert len(reviewer.calls) == 1


def test_single_instance_lock(tmp_path: Path) -> None:
    lock1 = SingleInstanceLock("proj_lock", lock_dir=tmp_path)
    assert lock1.acquire() is True

    # Second lock on same project cannot be acquired
    lock2 = SingleInstanceLock("proj_lock", lock_dir=tmp_path)
    assert lock2.acquire() is False

    lock1.release()
    # Now lock2 can acquire
    assert lock2.acquire() is True
    lock2.release()
