"""Comprehensive tests for BLUEPRINT 5c, 5d, 5e, 5f guards, self-healing, recovery, deck, and release."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nightshift.engine.ci import CiPipelineManager
from nightshift.engine.deck import generate_deck_data, write_morning_digest
from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.release import ReleaseManager, compute_sha256
from nightshift.engine.scheduler import Scheduler
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig, set_keep_awake
from nightshift.engine.vcs import FakeGitGh
from nightshift.engine.verify import RiskAssessment, check_diff_guards
from nightshift.events import Event, EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners import FakeRunner, RunnerResult
from nightshift.store import Store


def _ok_login_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    return (0, '{"loggedIn": true}\nLogged in to github.com\nOK', "", False)


def test_diff_guard_allows_modifying_test_file_but_blocks_deletion(tmp_path: Path) -> None:
    modify_diff = """
diff --git a/backend/tests/test_sample.py b/backend/tests/test_sample.py
--- a/backend/tests/test_sample.py
+++ b/backend/tests/test_sample.py
@@ -1,2 +1,3 @@
 def test_ok():
     assert True
+    assert 1 + 1 == 2
"""
    findings = check_diff_guards(tmp_path, ["backend/tests/test_sample.py"], modify_diff)
    assert not any(f.guard == "no_deleted_tests" for f in findings)

    delete_diff = """
diff --git a/backend/tests/test_sample.py b/backend/tests/test_sample.py
deleted file mode 100644
--- a/backend/tests/test_sample.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_ok():
-    assert True
"""
    del_findings = check_diff_guards(tmp_path, ["backend/tests/test_sample.py"], delete_diff)
    assert any(f.guard == "no_deleted_tests" for f in del_findings)


@pytest.mark.asyncio
async def test_diff_guard_self_heal_sends_findings_back_to_implementer(tmp_path: Path) -> None:
    db = tmp_path / "self_heal.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()

        attempt_count = 0

        def on_call(prompt: str, wd: Path, model: str | None, effort: str) -> None:
            nonlocal attempt_count
            attempt_count += 1
            vcs.add_pr("feat/p1", number=501)
            if attempt_count == 1:
                # First attempt introduces a skipped test -> trips diff guard
                vcs.diffs["feat/p1"] = "--- a/tests/t.py\n+++ b/tests/t.py\n+@pytest.mark.skip\n def test_x(): pass\n"
                vcs.changed_files["feat/p1"] = ["tests/t.py"]
            else:
                # Second attempt sees DIFF GUARD FINDINGS in prompt and fixes it
                prompt_text = (wd / ".nightshift_prompt.md").read_text(encoding="utf-8")
                assert "DIFF GUARD FINDINGS TO FIX" in prompt_text
                assert "no_skipped_tests" in prompt_text
                vcs.diffs["feat/p1"] = "--- a/tests/t.py\n+++ b/tests/t.py\n+def test_x(): assert True\n"
                vcs.changed_files["feat/p1"] = ["tests/t.py"]

        runners = {"agy": FakeRunner(name="agy", on_call=on_call)}
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, events, quota, vcs=vcs, runners=runners, login_checker=logins)

        project = ProjectConfig(id="p_heal", name="Heal", repo="o/r", local_path=tmp_path / "repo", output_dir=tmp_path / "out")
        project.local_path.mkdir()
        phases = [PhaseDefinition(id="p1", title="P1", branch="feat/p1", prompt="Implement P1")]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
                rows = await engine.execute_plan(project, phases, plan_id="plan_heal")

        assert rows[0]["status"] == "COMPLETED"
        assert attempt_count == 2
        guards = store.list_guard_results("p1")
        assert any(g["guard"] == "no_skipped_tests" and g["passed"] == 0 for g in guards)


@pytest.mark.asyncio
async def test_hard_guard_no_push_to_main(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    with pytest.raises(ValueError, match="protected branch 'main'"):
        vcs.create_checkpoint_commit_and_push(tmp_path, "main", "bad push")

    db = tmp_path / "no_main.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, events, quota, vcs=vcs, runners={"agy": FakeRunner.success("agy")}, login_checker=logins)
        project = ProjectConfig(id="p_main", name="MainGuard", repo="o/r", local_path=tmp_path / "repo", output_dir=tmp_path / "out")
        project.local_path.mkdir()
        phases = [PhaseDefinition(id="p1", title="Bad Phase", branch="main", prompt="Never push main")]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
                with pytest.raises(ValueError, match="protected branch 'main'"):
                    await engine.execute_plan(project, phases, plan_id="plan_main")


@pytest.mark.asyncio
async def test_hard_guard_exact_head_sha_before_merge(tmp_path: Path) -> None:
    db = tmp_path / "sha_guard.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        vcs.add_pr("feat/p1", number=601, head_sha="sha_checked_green")

        # Mutate headRefOid right after watch_pr_checks passes (simulating unverified push)
        orig_watch = vcs.watch_pr_checks

        def mutating_watch(repo: str, pr_number: int, wt: Path, timeout_seconds: int = 3600) -> tuple[bool, str]:
            ok, msg = orig_watch(repo, pr_number, wt, timeout_seconds)
            # First find_open_pr inside watch_and_heal captures checked_sha, then we mutate before merge
            return ok, msg

        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, events, quota, vcs=vcs, runners={"agy": FakeRunner.success("agy")}, login_checker=logins)

        orig_watch_and_heal = engine.ci_pipeline.watch_and_heal

        async def watch_then_mutate(*args, **kwargs):
            res = await orig_watch_and_heal(*args, **kwargs)
            vcs.prs["feat/p1"]["headRefOid"] = "sha_unverified_sneak"
            return res

        engine.ci_pipeline.watch_and_heal = watch_then_mutate  # type: ignore[method-assign]

        project = ProjectConfig(id="p_sha", name="ShaGuard", repo="o/r", local_path=tmp_path / "repo", output_dir=tmp_path / "out")
        project.local_path.mkdir()
        phases = [PhaseDefinition(id="p1", title="P1", branch="feat/p1")]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
                rows = await engine.execute_plan(project, phases, plan_id="plan_sha")

        assert rows[0]["status"] == "PARKED"
        assert "head SHA changed" in rows[0]["detail"]
        assert "feat/p1" not in vcs.merged_prs


@pytest.mark.asyncio
async def test_merge_main_conflict_resolution_in_ci_fix_loop(tmp_path: Path) -> None:
    db = tmp_path / "conflict.db"
    with Store(db) as store:
        store.migrate()
        vcs = FakeGitGh()
        vcs.ci_results[701] = [False, False, True]
        vcs.merge_conflict = True
        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)

        saw_conflict = False

        def on_fix(prompt: str, wd: Path, model: str | None, effort: str) -> None:
            nonlocal saw_conflict
            text = (wd / ".nightshift_prompt.md").read_text(encoding="utf-8")
            if "MERGE CONFLICT with origin/main" in text:
                saw_conflict = True
                vcs.merge_conflict = False

        runners = {"agy": FakeRunner(name="agy", on_call=on_fix)}
        ci_mgr = CiPipelineManager(vcs, store, sched, runners)
        wt = tmp_path / "wt"
        wt.mkdir()

        res = await ci_mgr.watch_and_heal(
            repo="o/r",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="P1",
            branch="feat/p1",
            pr_number=701,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
        )
        assert res.passed is True
        assert saw_conflict is True


@pytest.mark.asyncio
async def test_parking_continues_independent_and_stacks_dependent_phases(tmp_path: Path) -> None:
    db = tmp_path / "stacked.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()

        prepared_bases: dict[str, str] = {}
        orig_prepare = vcs.prepare_worktree

        def tracking_prepare(local_repo: Path, worktree_dir: Path, branch: str, base_branch: str = "main") -> Path:
            prepared_bases[branch] = base_branch
            return orig_prepare(local_repo, worktree_dir, branch, base_branch=base_branch)

        vcs.prepare_worktree = tracking_prepare  # type: ignore[method-assign]

        def on_call(prompt: str, wd: Path, model: str | None, effort: str) -> None:
            # p1 never opens a PR -> NO-OP -> parks; p2 and p3 succeed
            if "p2" in str(wd):
                vcs.add_pr("feat/p2", number=802)
            elif "p3" in str(wd):
                vcs.add_pr("feat/p3", number=803)

        runners = {"agy": FakeRunner(name="agy", on_call=on_call)}
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, events, quota, vcs=vcs, runners=runners, login_checker=logins)
        engine.scheduler.max_implement_attempts = 1

        project = ProjectConfig(id="p_stack", name="Stack", repo="o/r", local_path=tmp_path / "repo", output_dir=tmp_path / "out")
        project.local_path.mkdir()
        phases = [
            PhaseDefinition(id="p1", title="P1 Parked", branch="feat/p1", position=1),
            PhaseDefinition(id="p2", title="P2 Stacked", branch="feat/p2", depends_on=["p1"], position=2),
            PhaseDefinition(id="p3", title="P3 Independent", branch="feat/p3", position=3),
        ]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
                rows = await engine.execute_plan(project, phases, plan_id="plan_stack")

        by_id = {r["phase_id"]: r["status"] for r in rows}
        assert by_id["p1"] == "PARKED"
        assert by_id["p2"] == "COMPLETED"
        assert by_id["p3"] == "COMPLETED"
        assert prepared_bases["feat/p2"] == "feat/p1"
        assert prepared_bases["feat/p3"] == "main"

        parked_p1 = store.get_parked("p1")
        assert parked_p1 is not None
        assert "p2" in json.loads(parked_p1["stacked_children_json"])


@pytest.mark.asyncio
async def test_runaway_limit_max_run_invocations(tmp_path: Path) -> None:
    db = tmp_path / "runaway.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, events, quota, vcs=vcs, runners={"agy": FakeRunner.no_op("agy")}, login_checker=logins)
        engine.scheduler.max_run_invocations = 2
        engine.scheduler.max_implement_attempts = 5

        project = ProjectConfig(id="p_cap", name="Cap", repo="o/r", local_path=tmp_path / "repo", output_dir=tmp_path / "out")
        project.local_path.mkdir()
        phases = [PhaseDefinition(id="p1", title="P1", branch="feat/p1")]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
                rows = await engine.execute_plan(project, phases, plan_id="plan_cap")

        assert rows[0]["status"] == "PARKED"
        assert "Max AI invocations" in rows[0]["detail"]
        assert engine.scheduler.total_invocations_count == 2


def test_budget_cap_and_logged_out_handoff_and_aggressive_switchback(tmp_path: Path) -> None:
    db = tmp_path / "sched_guards.db"
    with Store(db) as store:
        store.migrate()
        quota = QuotaLedger(store)

        # Configure agy with a 1-run daily budget cap and record 1 run -> agy hits budget cap
        store.upsert_engine("eng-agy", "agy", "", budget_json=json.dumps({"max_runs_today": 1}))
        quota.record_usage("agy", "default", tokens_in=100, tokens_out=100)

        # Configure gemini as logged out -> priority list must skip agy (budget) and gemini (logged out) -> codex!
        def mock_login(cmd: list[str], to: int) -> tuple[int, str, str, bool]:
            if cmd[0] == "gemini":
                return (1, "", "Error: Not authenticated", False)
            return _ok_login_runner(cmd, to)

        with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
            logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=mock_login)
            sched = Scheduler(store, quota, login_checker=logins, aggressive_switchback=True)

            store.create_project("proj", "P", "o/r", str(tmp_path))
            store.create_plan("plan", "proj", "Plan")
            store.upsert_phase("p1", "plan", "P1", "feat/p1")

            choice = sched.select_engine("p1")
            assert choice is not None
            assert choice.engine == "codex"

            # Aggressive switch-back caps fallback invocation at 30 min (1800s) when preferred is cooling down
            store.upsert_phase_assignment("p1", override_engine="claude")
            quota.record_quota_error("claude", "default", "Rate limit exceeded. Try again in 45 minutes.")
            assert sched.invocation_timeout_seconds("p1", "codex", default_timeout_seconds=7200) == 1800
            assert sched.invocation_timeout_seconds("p1", "claude", default_timeout_seconds=7200) == 7200


def test_cooldown_expiry_restores_availability(tmp_path: Path) -> None:
    db = tmp_path / "cooldown_exp.db"
    with Store(db) as store:
        store.migrate()
        quota = QuotaLedger(store)
        past_reset = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        store.record_ledger_entry("gemini", "default", "quota_error", reset_at=past_reset)
        assert quota.is_cooling_down("gemini") is False
        assert quota.get_cooldown_remaining("gemini") == 0.0


@pytest.mark.asyncio
async def test_network_loss_backoff_without_burning_attempts_and_branch_protection_warn(tmp_path: Path) -> None:
    db = tmp_path / "net_backoff.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        vcs.branch_protection["main"] = False  # Triggers branch_protection.missing warning

        git_calls = 0

        def flaky_net_login(cmd: list[str], to: int) -> tuple[int, str, str, bool]:
            nonlocal git_calls
            if cmd[0] == "git":
                git_calls += 1
                if git_calls == 1:
                    return (128, "", "fatal: unable to access: Could not resolve host: github.com", False)
            return _ok_login_runner(cmd, to)

        def on_call(prompt: str, wd: Path, model: str | None, effort: str) -> None:
            vcs.add_pr("feat/p1", number=901)

        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=flaky_net_login)
        engine = EngineStateMachine(
            store, events, quota, vcs=vcs, runners={"agy": FakeRunner(name="agy", on_call=on_call)},
            login_checker=logins, network_backoff_seconds=0.01,
        )

        project = ProjectConfig(id="p_net", name="Net", repo="o/r", local_path=tmp_path / "repo", output_dir=tmp_path / "out")
        project.local_path.mkdir()
        phases = [PhaseDefinition(id="p1", title="P1", branch="feat/p1")]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
                rows = await engine.execute_plan(project, phases, plan_id="plan_net")

        assert rows[0]["status"] == "COMPLETED"
        assert git_calls == 2
        # Attempt counter was NOT burned by the network backoff!
        assert store.get_phase("p1")["attempt"] == 1
        all_events = store.list_events()
        assert any(e["kind"] == "preflight.backoff" for e in all_events)
        assert any(e["kind"] == "branch_protection.missing" and e["level"] == "warn" for e in all_events)


def test_keep_awake_windows_api() -> None:
    mock_kernel32 = MagicMock()
    mock_windll = MagicMock(kernel32=mock_kernel32)
    with patch("nightshift.engine.state_machine.sys.platform", "win32"):
        with patch("nightshift.engine.state_machine.ctypes.windll", mock_windll, create=True):
            assert set_keep_awake(True) is True
            mock_kernel32.SetThreadExecutionState.assert_called_with(0x80000000 | 0x00000001)
            assert set_keep_awake(False) is True
            mock_kernel32.SetThreadExecutionState.assert_called_with(0x80000000)


@pytest.mark.asyncio
async def test_event_secret_redaction_and_raw_log_files_and_hash_chain(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    with Store(db) as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()

        secret_tok = "ghp_123456789012345678901234567890123456"
        events.emit(Event(kind="test.secret", plain_text=f"Leaked token {secret_tok} here"))

        def on_call(prompt: str, wd: Path, model: str | None, effort: str) -> None:
            vcs.add_pr("feat/p1", number=950)

        runner = FakeRunner(
            name="agy",
            responses=[RunnerResult(rc=0, stdout=f"Done with {secret_tok}", stderr="")],
            on_call=on_call,
        )
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        engine = EngineStateMachine(store, events, quota, vcs=vcs, runners={"agy": runner}, login_checker=logins)

        out_dir = tmp_path / "out"
        project = ProjectConfig(id="p_aud", name="Audit", repo="o/r", local_path=tmp_path / "repo", output_dir=out_dir)
        project.local_path.mkdir()
        phases = [PhaseDefinition(id="p1", title="P1", branch="feat/p1")]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"):
            with patch("nightshift.engine.logins.resolve_bin", return_value="tool.exe"):
                await engine.execute_plan(project, phases, plan_id="plan_aud")

        assert store.verify_event_chain() is True
        all_events = store.list_events()
        for ev in all_events:
            assert secret_tok not in ev["plain_text"]

        completed_ev = [e for e in all_events if e["kind"] == "runner.completed"][0]
        assert completed_ev["raw_path"] is not None
        raw_content = Path(completed_ev["raw_path"]).read_text(encoding="utf-8")
        assert secret_tok not in raw_content
        assert "[REDACTED_SECRET]" in raw_content


@pytest.mark.asyncio
async def test_fake_runner_timeout_and_crash_and_noop_modes(tmp_path: Path) -> None:
    r_timeout = FakeRunner.timeout("agy", seconds=60)
    res_to = await r_timeout.run("p", tmp_path)
    assert res_to.timed_out is True and res_to.rc == 124

    r_crash = FakeRunner.crash("gemini", rc=2, stderr="segfault")
    res_cr = await r_crash.run("p", tmp_path)
    assert res_cr.rc == 2 and "segfault" in res_cr.stderr

    r_noop = FakeRunner.no_op("codex")
    res_no = await r_noop.run("p", tmp_path)
    assert res_no.rc == 0

    r_quota = FakeRunner.quota_error("claude", reset_at="2026-11-25T05:00:00Z")
    res_q = await r_quota.run("p", tmp_path)
    assert res_q.quota_error is True and res_q.reset_at == "2026-11-25T05:00:00Z"


def test_deck_data_max_3_needs_you_and_digest(tmp_path: Path) -> None:
    db = tmp_path / "deck.db"
    with Store(db) as store:
        store.migrate()
        store.create_project("proj_d", "NightShift", "o/r", str(tmp_path))
        store.create_plan("plan_d", "proj_d", "Plan")
        for i in range(1, 6):
            store.upsert_phase(f"p{i}", "plan_d", f"Phase {i}", f"feat/p{i}", status="parked" if i <= 4 else "merged")
        for i in range(1, 5):
            store.park_phase(f"p{i}", reason=f"Reason {i}", next_step=f"Fix {i}")

        store.record_ledger_entry("agy", "default", "run", tokens_in=500, tokens_out=300)
        store.record_handoff("p1", "claude", "agy", "Quota hit", "sha1234")

        deck = generate_deck_data(store, "proj_d")
        assert deck["state"] == "attention"
        assert len(deck["needs_you"]) == 3  # Max 3 items per BLUEPRINT 5e
        assert len(deck["done_tonight"]) == 1
        assert deck["handoff_count"] == 1
        assert deck["usage_tonight"]["agy"]["tokens"] == 800

        digest_path = write_morning_digest(tmp_path / "out", "NightShift", [{"title": "Phase 5", "runner": "agy", "status": "COMPLETED", "detail": "Merged"}], deck)
        assert digest_path.exists()
        content = digest_path.read_text(encoding="utf-8")
        assert "Attention Required" in content
        assert "Handoffs triggered" in content


def test_release_manager_sha256sums_and_output_copy(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    vcs.artifacts_to_create = ["NightShift-v1.zip"]
    mgr = ReleaseManager(vcs)
    out_dir = tmp_path / "releases"

    tag, files = mgr.publish_verified_release(
        repo="owner/nightshift",
        output_dir=out_dir,
        title_note="Phase 3 release",
        workflow_name="Desktop build",
        artifact_glob="*.zip",
        tag_prefix="v1.0.0-preview.",
    )
    assert tag == "v1.0.0-preview.1"
    assert len(files) == 2
    zip_out = out_dir / "NightShift-v1.zip"
    sums_out = out_dir / "v1.0.0-preview.1-SHA256SUMS.txt"
    assert zip_out.exists()
    assert sums_out.exists()
    expected_sha = compute_sha256(zip_out)
    assert f"{expected_sha}  NightShift-v1.zip" in sums_out.read_text(encoding="utf-8")
    assert len(vcs.releases) == 1
