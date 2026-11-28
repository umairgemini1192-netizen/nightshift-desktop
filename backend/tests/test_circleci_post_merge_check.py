"""Post-merge main-build check for CircleCI-only projects.

Found live on 2026-11-28: the post-merge check only looked for a GitHub Actions run named by
``release_workflow`` (default "Android build"). nightshift-desktop has no GitHub Actions at all
(CircleCI only), so every auto-merged phase was merged and then parked with
"Could not verify main build", stalling the plan after each merge.

For projects that build on CircleCI only (``.circleci/config.yml`` present and no
``.github/workflows``), or that set ``release_workflow`` to "circleci", the engine must wait for
the required CircleCI jobs on the exact merge commit instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.pipeline_contracts import MergeGate
from nightshift.engine.pipeline_evidence import CiJobCheck
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
from nightshift.engine.vcs import FakeGitGh
from nightshift.events import EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.fake import FakeRunner
from nightshift.store import Store

REQUIRED = ["desktop-gates", "desktop-backend", "desktop-frontend", "desktop-package"]
MERGE_SHA = "sha_merge_123"  # FakeGitGh.merge_pr always produces this merge commit


def _ok_login_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    return (0, '{"loggedIn": true}\nLogged in to github.com\nOK', "", False)


def _jobs(sha: str, *, branch: str, status: str = "success", overrides: dict[str, str] | None = None) -> list[CiJobCheck]:
    overrides = overrides or {}
    return [
        CiJobCheck(
            name=name,
            status=overrides.get(name, status),
            commit_sha=sha,
            build_id=f"b-{i}",
            duration_seconds=30.0 if name == "desktop-gates" else 120.0,
            is_gate_job=name == "desktop-gates",
            branch=branch,
        )
        for i, name in enumerate(REQUIRED, start=1)
    ]


class _SequencedChecks(FakeGitGh):
    """FakeGitGh whose post-merge checks change between polls (e.g. running -> success)."""

    def __init__(self, sequence: list[list[CiJobCheck]]) -> None:
        super().__init__()
        self._sequence = list(sequence)
        self.post_merge_polls = 0

    def get_commit_ci_checks(self, repo: str, commit_sha: str, branch: str = "") -> list[CiJobCheck]:
        if commit_sha == MERGE_SHA:
            self.post_merge_polls += 1
            if len(self._sequence) > 1:
                return list(self._sequence.pop(0))
            return list(self._sequence[0])
        return super().get_commit_ci_checks(repo, commit_sha, branch)


def _forbid_actions_lookup(vcs: FakeGitGh) -> list[str]:
    calls: list[str] = []

    def _watch(*args: Any, **kwargs: Any) -> tuple[bool | None, str, int | None]:
        calls.append(str(kwargs.get("workflow") or args[2:3]))
        return None, "No workflow runs found", None

    vcs.watch_workflow_run = _watch  # type: ignore[method-assign]
    return calls


def _engine(tmp_path: Path, vcs: FakeGitGh) -> tuple[Store, EngineStateMachine]:
    store = Store(tmp_path / "engine_pm.db")
    store.migrate()
    events = EventBus(store)
    quota = QuotaLedger(store)
    logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
    engine = EngineStateMachine(
        store=store,
        event_bus=events,
        quota_ledger=quota,
        vcs=vcs,
        runners={
            "agy": FakeRunner.success("agy", stdout="Implemented\n"),
            "codex": FakeRunner.success("codex", stdout="LGTM. Clean change.\n"),
        },
        login_checker=logins,
        merge_gate=MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=False),
    )
    engine._ci_poll_sleep = lambda _s: None  # never really sleep in tests
    return store, engine


def _circleci_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".circleci").mkdir(parents=True)
    (repo / ".circleci" / "config.yml").write_text("version: 2.1\n", encoding="utf-8")
    return repo


def _project(tmp_path: Path, repo: Path, *, release_workflow: str = "Android build") -> ProjectConfig:
    return ProjectConfig(
        id="proj-pm",
        name="NightShift",
        repo="owner/nightshift",
        local_path=repo,
        output_dir=tmp_path / "out",
        artifact_glob="*.zip",
        release_workflow=release_workflow,
        required_ci_jobs=list(REQUIRED),
    )


def _phase(branch: str, *, release: bool = False) -> PhaseDefinition:
    return PhaseDefinition(
        id="pm-phase",
        title="Post-merge CircleCI check",
        prompt="Implement feature",
        branch=branch,
        auto_merge=True,
        release_after_merge=release,
    )


@pytest.mark.asyncio
async def test_circleci_only_project_completes_without_github_actions(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    vcs.add_pr("feat/pm-ok", number=901, head_sha="sha_head_901")
    vcs.commit_ci_checks["sha_head_901"] = _jobs("sha_head_901", branch="feat/pm-ok")
    vcs.commit_ci_checks[MERGE_SHA] = _jobs(MERGE_SHA, branch="main")
    actions_calls = _forbid_actions_lookup(vcs)
    store, engine = _engine(tmp_path, vcs)
    try:
        rows = await engine.execute_plan(_project(tmp_path, _circleci_repo(tmp_path)), [_phase("feat/pm-ok")], plan_id="plan-pm")
        assert rows[0]["status"] == "COMPLETED", rows[0]
        assert "feat/pm-ok" in vcs.merged_prs
        assert actions_calls == []  # never looked for a GitHub Actions run
        assert store.get_phase("pm-phase")["status"] == "completed"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_circleci_check_waits_while_jobs_are_running(tmp_path: Path) -> None:
    running = _jobs(MERGE_SHA, branch="main", overrides={"desktop-package": "running"})
    vcs = _SequencedChecks([running, running, _jobs(MERGE_SHA, branch="main")])
    vcs.add_pr("feat/pm-wait", number=902, head_sha="sha_head_902")
    vcs.commit_ci_checks["sha_head_902"] = _jobs("sha_head_902", branch="feat/pm-wait")
    _forbid_actions_lookup(vcs)
    store, engine = _engine(tmp_path, vcs)
    try:
        rows = await engine.execute_plan(_project(tmp_path, _circleci_repo(tmp_path)), [_phase("feat/pm-wait")], plan_id="plan-pm")
        assert rows[0]["status"] == "COMPLETED", rows[0]
        assert vcs.post_merge_polls >= 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_circleci_check_waits_for_missing_required_job(tmp_path: Path) -> None:
    partial = [j for j in _jobs(MERGE_SHA, branch="main") if j.name != "desktop-package"]
    vcs = _SequencedChecks([partial, _jobs(MERGE_SHA, branch="main")])
    vcs.add_pr("feat/pm-missing", number=906, head_sha="sha_head_906")
    vcs.commit_ci_checks["sha_head_906"] = _jobs("sha_head_906", branch="feat/pm-missing")
    _forbid_actions_lookup(vcs)
    store, engine = _engine(tmp_path, vcs)
    try:
        rows = await engine.execute_plan(_project(tmp_path, _circleci_repo(tmp_path)), [_phase("feat/pm-missing")], plan_id="plan-pm")
        assert rows[0]["status"] == "COMPLETED", rows[0]
        assert vcs.post_merge_polls >= 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_circleci_failed_main_job_is_not_completed(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    vcs.add_pr("feat/pm-fail", number=903, head_sha="sha_head_903")
    vcs.commit_ci_checks["sha_head_903"] = _jobs("sha_head_903", branch="feat/pm-fail")
    vcs.commit_ci_checks[MERGE_SHA] = _jobs(MERGE_SHA, branch="main", overrides={"desktop-backend": "failed"})
    _forbid_actions_lookup(vcs)
    store, engine = _engine(tmp_path, vcs)
    try:
        rows = await engine.execute_plan(_project(tmp_path, _circleci_repo(tmp_path)), [_phase("feat/pm-fail")], plan_id="plan-pm")
        assert rows[0]["status"] == "PARKED", rows[0]
        assert store.get_phase("pm-phase")["status"] != "completed"
        assert len(vcs.releases) == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_circleci_check_times_out_and_parks(tmp_path: Path) -> None:
    running = _jobs(MERGE_SHA, branch="main", overrides={"desktop-package": "running"})
    vcs = _SequencedChecks([running])
    vcs.add_pr("feat/pm-slow", number=904, head_sha="sha_head_904")
    vcs.commit_ci_checks["sha_head_904"] = _jobs("sha_head_904", branch="feat/pm-slow")
    _forbid_actions_lookup(vcs)
    store, engine = _engine(tmp_path, vcs)
    clock = {"t": 0.0}

    def _advance(_s: float) -> None:
        clock["t"] += 600.0

    engine._ci_poll_sleep = _advance
    engine._ci_clock = lambda: clock["t"]
    try:
        rows = await engine.execute_plan(_project(tmp_path, _circleci_repo(tmp_path)), [_phase("feat/pm-slow")], plan_id="plan-pm")
        assert rows[0]["status"] == "PARKED", rows[0]
        assert "Could not verify main build" in rows[0]["detail"]
        assert "desktop-package" in rows[0]["detail"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_circleci_release_after_merge_fails_closed(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    vcs.add_pr("feat/pm-rel", number=905, head_sha="sha_head_905")
    vcs.commit_ci_checks["sha_head_905"] = _jobs("sha_head_905", branch="feat/pm-rel")
    vcs.commit_ci_checks[MERGE_SHA] = _jobs(MERGE_SHA, branch="main")
    _forbid_actions_lookup(vcs)
    store, engine = _engine(tmp_path, vcs)
    try:
        rows = await engine.execute_plan(
            _project(tmp_path, _circleci_repo(tmp_path)), [_phase("feat/pm-rel", release=True)], plan_id="plan-pm"
        )
        assert rows[0]["status"] == "PARKED", rows[0]
        assert "CircleCI" in rows[0]["detail"]
        assert len(vcs.releases) == 0
    finally:
        store.close()


def test_main_check_mode_detection(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path, FakeGitGh())
    try:
        circle = _circleci_repo(tmp_path)
        assert engine._uses_circleci_main_check(_project(tmp_path, circle)) is True

        both = tmp_path / "both"
        (both / ".circleci").mkdir(parents=True)
        (both / ".circleci" / "config.yml").write_text("version: 2.1\n", encoding="utf-8")
        (both / ".github" / "workflows").mkdir(parents=True)
        assert engine._uses_circleci_main_check(_project(tmp_path, both)) is False

        plain = tmp_path / "plain"
        plain.mkdir()
        assert engine._uses_circleci_main_check(_project(tmp_path, plain)) is False
        assert engine._uses_circleci_main_check(_project(tmp_path, plain, release_workflow="circleci")) is True
    finally:
        store.close()
