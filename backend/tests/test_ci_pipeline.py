"""Tests for CI watching, flaky detection, auto-fix rounds, review findings, and post-merge reverts (BLUEPRINT 5d)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightshift.engine.ci import CiPipelineManager, parse_review_findings
from nightshift.engine.scheduler import Scheduler
from nightshift.engine.vcs import FakeGitGh
from nightshift.engine.verify import RiskAssessment
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners import FakeRunner, RunnerResult
from nightshift.store import Store


def test_parse_review_findings() -> None:
    text = """
    Review summary:
    - [P1] Null pointer exception when user profile is null.
    - [P2] Consider extracting constant for timeout.
    - Blocking bug: Activity does not implement back handler.
    - Nit: Add comments explaining regex.
    """
    findings = parse_review_findings(text)
    p1s = [f for f in findings if f.priority == "P1"]
    p2s = [f for f in findings if f.priority == "P2"]

    assert len(p1s) >= 2
    assert len(p2s) >= 2


@pytest.mark.asyncio
async def test_ci_watch_clean_pass(tmp_path: Path) -> None:
    db = tmp_path / "ci_clean.db"
    with Store(db) as store:
        store.migrate()
        vcs = FakeGitGh()
        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)
        runners = {"agy": FakeRunner.success("agy")}

        ci_mgr = CiPipelineManager(vcs, store, sched, runners)
        wt = tmp_path / "wt"
        wt.mkdir()

        res = await ci_mgr.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="Phase 1",
            branch="feat/p1",
            pr_number=101,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
        )
        assert res.passed is True
        assert res.rounds_used == 0
        assert res.flaky is False


@pytest.mark.asyncio
async def test_ci_flaky_test_detection(tmp_path: Path) -> None:
    db = tmp_path / "ci_flaky.db"
    with Store(db) as store:
        store.migrate()
        vcs = FakeGitGh()
        vcs.ci_flaky[102] = True  # First check fails, second check passes without code change

        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)
        runners = {"agy": FakeRunner.success("agy")}

        ci_mgr = CiPipelineManager(vcs, store, sched, runners)
        wt = tmp_path / "wt"
        wt.mkdir()

        res = await ci_mgr.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="Phase 1",
            branch="feat/p1",
            pr_number=102,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
        )
        assert res.passed is True
        assert res.flaky is True
        assert res.rounds_used == 0  # Does not burn a fix round!


@pytest.mark.asyncio
async def test_ci_auto_fix_and_engine_rotation(tmp_path: Path) -> None:
    db = tmp_path / "ci_rotate.db"
    with Store(db) as store:
        store.migrate()
        vcs = FakeGitGh()
        # Set checks for PR 103: Fail round 1 (with flaky re-check), Fail round 2, Pass round 3
        vcs.ci_results[103] = [False, False, False, True]

        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)

        # Setup runners: agy fails twice, then rotates to gemini which fixes it
        runners = {
            "agy": FakeRunner.success("agy"),
            "gemini": FakeRunner.success("gemini"),
        }

        ci_mgr = CiPipelineManager(vcs, store, sched, runners)
        wt = tmp_path / "wt"
        wt.mkdir()

        res = await ci_mgr.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="Phase 1",
            branch="feat/p1",
            pr_number=103,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
            max_retries=4,
        )
        assert res.passed is True
        assert res.rounds_used >= 2
        # Engine rotated away from agy to gemini
        assert res.last_engine == "gemini"


@pytest.mark.asyncio
async def test_risk_based_review_p1_blocks_p2_passes(tmp_path: Path) -> None:
    db = tmp_path / "ci_review.db"
    with Store(db) as store:
        store.migrate()
        vcs = FakeGitGh()
        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)

        # Reviewer outputs: First round finds [P1], Second round finds [P2] (clean)
        codex_reviewer = FakeRunner(
            name="codex",
            responses=[
                RunnerResult(rc=0, stdout="Code review:\n- [P1] Potential NPE in handleData", stderr=""),
                RunnerResult(rc=0, stdout="Code review:\n- [P2] Minor nit on spacing", stderr=""),
            ],
        )
        agy_implementer = FakeRunner.success("agy")
        runners = {"agy": agy_implementer, "codex": codex_reviewer}

        ci_mgr = CiPipelineManager(vcs, store, sched, runners)
        wt = tmp_path / "wt"
        wt.mkdir()

        # Phase with critical risk
        res = await ci_mgr.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="Phase 1",
            branch="feat/p1",
            pr_number=104,
            initial_engine="agy",
            risk=RiskAssessment(level="critical", reasons=["Modifies permissions"]),
        )
        assert res.passed is True
        # Verified that P1 triggered a fix round and re-review
        assert len(codex_reviewer.calls) >= 2


def test_post_merge_failure_auto_revert(tmp_path: Path) -> None:
    db = tmp_path / "post_merge.db"
    with Store(db) as store:
        store.migrate()
        vcs = FakeGitGh()
        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)

        store.create_project("proj-1", "Test", "owner/repo", str(tmp_path))
        store.create_plan("plan-1", "proj-1", "Plan")
        store.upsert_phase("p1", "plan-1", "Phase 1", "feat/p1")

        ci_mgr = CiPipelineManager(vcs, store, sched, {})
        ok, msg = ci_mgr.handle_post_merge_failure(
            local_repo=tmp_path,
            merge_commit_sha="sha_merge_123",
            phase_id="p1",
            failure_log="Gradle build failed on main branch",
        )
        assert ok is True
        assert "Reverted sha_mer" in msg

        # Reverts tracked in VCS
        assert len(vcs.reverts) == 1
        assert vcs.reverts[0]["commit_sha"] == "sha_merge_123"

        # Phase parked with post-merge failure details
        parked = store.get_parked("p1")
        assert parked is not None
        assert "Post-merge main build failed" in parked["reason"]
