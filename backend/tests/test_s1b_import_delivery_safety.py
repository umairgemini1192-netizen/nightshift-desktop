"""Reproducing tests for S1b import, delivery safety, CircleCI null-vcs, Codex review, and governance split."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nightshift.bridge import BridgeAPI
from nightshift.engine.ci import CiPipelineManager, CiWatchResult
from nightshift.engine.credentials import MemoryCredentialStore
from nightshift.engine.pipeline_contracts import (
    MergeGate,
    compute_contract_sha256,
    detect_assertion_relaxation_for_files,
)
from nightshift.engine.pipeline_evidence import (
    CiArtifact,
    CiJobCheck,
    persist_ci_evidence,
    validate_ci_evidence,
)
from nightshift.engine.pipeline_triage import get_small_job_ladder
from nightshift.engine.planner import PhaseDefinition, import_queue_json, validate_plan
from nightshift.engine.router import SEED_ENGINES, Router
from nightshift.engine.scheduler import Scheduler
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
from nightshift.engine.vcs import FakeGitGh, SubprocessGitGh
from nightshift.engine.verify import check_diff_guards
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.optimizer.repomix import resolve_git_info_exclude
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.base import RunnerResult
from nightshift.runners.codex import CodexRunner
from nightshift.runners.fake import FakeRunner
from nightshift.store import Store


def _make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.migrate()
    return store


def _seed_project_and_task(
    store: Store,
    tmp_path: Path,
    *,
    project_id: str = "owner/repo",
    phase_id: str = "p1",
    branch: str = "feat/p1",
    risk_class: str = "ui_cosmetic",
    require_codex_review: int = 1,
    allowed_globs: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    store.create_project(
        project_id,
        "Test Project",
        project_id,
        str(tmp_path),
        require_codex_review=require_codex_review,
    )
    store.create_plan("plan-1", project_id, "Plan 1")
    store.upsert_phase(
        phase_id=phase_id,
        plan_id="plan-1",
        title="Phase 1",
        branch=branch,
        require_codex_review=require_codex_review,
    )
    store.create_task_with_contract(
        task_id=f"task-{phase_id}",
        phase_id=phase_id,
        plan_id="plan-1",
        title="Phase 1",
        branch=branch,
        risk_class=risk_class,
        allowed_globs=allowed_globs or ["frontend/**"],
        forbidden_globs=[".circleci/**", ".github/**", "scripts/check_*"],
        owner_approval_required=False,
        require_codex_review=bool(require_codex_review),
    )
    task_row = store.get_task(f"task-{phase_id}") or {}
    contract_row = store.get_task_contract(str(task_row["contract_id"])) or {}
    return store.get_project(project_id) or {}, task_row, contract_row


# ---------------------------------------------------------------------------
# 1. Per-project ladders (import must not overwrite another project's ladders)
# ---------------------------------------------------------------------------

def test_per_project_ladders_do_not_overwrite_existing_project(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    bridge = BridgeAPI(store, EventBus(store), ModuleRegistry())

    q1 = {
        "repo": "owner/nightshift-desktop",
        "name": "NightShift Desktop",
        "failover_ladders": {"backend": ["agy", "gemini"], "small_job_ladder": ["qwen"]},
        "phases": [{"id": "ns-1", "title": "NS 1", "branch": "feat/ns-1", "ladder": "backend"}],
    }
    q2 = {
        "repo": "owner/drivelift-android",
        "name": "DriveLift Android",
        "failover_ladders": {"build": ["gemini", "codex"], "small_job_ladder": ["agy"]},
        "phases": [{"id": "dl-1", "title": "DL 1", "branch": "feat/dl-1", "ladder": "build"}],
    }

    r1 = bridge.call("plan.importQueue", {"queue": q1})
    assert r1["ok"] is True
    r2 = bridge.call("plan.importQueue", {"queue": q2})
    assert r2["ok"] is True

    ns_ladders = bridge.call("engines.getLadders", {"projectId": "owner/nightshift-desktop"})
    dl_ladders = bridge.call("engines.getLadders", {"projectId": "owner/drivelift-android"})
    assert ns_ladders["ok"] is True
    assert dl_ladders["ok"] is True
    assert "backend" in ns_ladders["data"]["ladders"]
    assert ns_ladders["data"]["ladders"]["small_job_ladder"] == [{"runner": "qwen", "model": None, "effort": None}]
    assert dl_ladders["data"]["ladders"]["small_job_ladder"] == [{"runner": "agy", "model": None, "effort": None}]

    val_ns = bridge.call("plan.validate", {"projectId": "owner/nightshift-desktop", "planId": "plan_owner/nightshift-desktop"})
    assert val_ns["ok"] is True
    assert val_ns["data"]["valid"] is True


# ---------------------------------------------------------------------------
# 2. small_job_ladder shape & credential/free_only_mode filtering
# ---------------------------------------------------------------------------

def test_small_job_ladder_accepts_objects_and_filters_unavailable_and_paid(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.set_setting("pipeline.freeOnlyMode", "1")
    store.upsert_engine("qwen", "qwen", "qwen-2.5-coder-32b", cost_tier="economy", enabled=1)
    store.upsert_engine("deepseek", "claude-compatible", "deepseek-chat", cost_tier="economy", enabled=1)
    store.upsert_engine("agy", "agy", "auto", cost_tier="standard", enabled=0)  # disabled
    store.upsert_engine("gemini", "gemini", "gemini-2.5-pro", cost_tier="standard", enabled=1)
    store.upsert_engine("claude", "claude", "claude-3-7-sonnet", cost_tier="gold", enabled=1)

    store.set_setting(
        "engines.ladders",
        json.dumps(
            {
                "small_job_ladder": [
                    {"runner": "deepseek", "model": "deepseek-chat"},
                    {"runner": "agy", "model": "auto"},
                    {"runner": "claude", "model": "claude-3-7-sonnet"},
                    {"runner": "qwen", "model": "qwen-2.5-coder-32b"},
                    "gemini",
                ]
            }
        ),
    )

    ladder = get_small_job_ladder(store)
    assert "deepseek" not in ladder  # paid API / violates free_only_mode / no credentials
    assert "claude" not in ladder    # gold / violates free_only_mode
    assert "agy" not in ladder       # disabled
    assert "qwen" in ladder
    assert "gemini" in ladder


# ---------------------------------------------------------------------------
# 3. Historic merged phases on first run after import -> no release side effects
# ---------------------------------------------------------------------------

def test_historic_merged_phase_after_import_has_no_release_side_effects(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    events = EventBus(store)
    vcs = FakeGitGh()
    # Pre-existing merged PR from BEFORE the import
    vcs.merged_prs[("owner/repo", "feat/old-phase")] = {
        "number": 42,
        "mergeCommit": {"oid": "oldsha42"},
        "mergedAt": "2026-11-20T10:00:00Z",
    }

    store.create_project("owner/repo", "Test", "owner/repo", str(tmp_path))
    store.create_plan("plan-1", "owner/repo", "Imported Plan")
    store.upsert_phase(
        phase_id="p-old",
        plan_id="plan-1",
        title="Old Merged Phase",
        branch="feat/old-phase",
        status="pending",
        release_after_merge=1,
    )

    sm = EngineStateMachine(
        store=store,
        event_bus=events,
        quota_ledger=QuotaLedger(store),
        vcs=vcs,
        runners={"agy": FakeRunner("agy")},
    )
    proj = ProjectConfig(
        id="owner/repo",
        name="Test",
        repo="owner/repo",
        local_path=tmp_path,
        output_dir=tmp_path / "dist",
    )
    phase = PhaseDefinition(
        id="p-old",
        title="Old Merged Phase",
        branch="feat/old-phase",
        release_after_merge=True,
    )

    res = sm._finish_merged_delivery(proj, phase, vcs.merged_prs[("owner/repo", "feat/old-phase")], "none")
    assert res["status"] == "SKIPPED"
    assert "historic merge, no delivery" in res["detail"].lower()
    assert len(vcs.releases) == 0
    assert (store.get_phase("p-old") or {}).get("status") == "merged"
    ev_texts = [e["plain_text"].lower() for e in store.list_events(limit=20)]
    assert any("historic merge, no delivery" in t for t in ev_texts)


# ---------------------------------------------------------------------------
# 4. CircleCI null-vcs pipeline artifact lookup via v1.1 job vcs_revision
# ---------------------------------------------------------------------------

def test_circleci_null_vcs_pipeline_resolves_via_v11_vcs_revision(tmp_path: Path) -> None:
    head_sha = "abc1234567890def"
    wf_id = "11111111-2222-3333-4444-555555555555"
    job_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    pipe_id = "99999999-8888-7777-6666-555555555555"

    def fake_gh(args: list[str], timeout: int = 60) -> tuple[int, str, str, bool]:
        url = args[2] if len(args) > 2 else ""
        if "/check-runs" in url:
            return 0, json.dumps({
                "check_runs": [
                    {
                        "name": "ci/circleci: android-build",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": head_sha,
                        "details_url": f"https://circleci.com/workflow/{wf_id}/job/{job_uuid}",
                    }
                ]
            }), "", False
        if "/status" in url:
            return 0, json.dumps({"statuses": []}), "", False
        return 0, "{}", "", False

    def fake_http_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any] | None:
        if f"/api/v2/workflow/{wf_id}/job" in url:
            return {
                "items": [
                    {
                        "id": job_uuid,
                        "name": "android-build",
                        "job_number": 77,
                        "status": "success",
                        "started_at": "2026-11-28T01:00:00Z",
                        "stopped_at": "2026-11-28T01:02:00Z",
                        "project_slug": "gh/owner/repo",
                    }
                ]
            }
        if f"/api/v2/workflow/{wf_id}" in url:
            return {"id": wf_id, "pipeline_id": pipe_id}
        if f"/api/v2/pipeline/{pipe_id}" in url:
            # Native CircleCI pipeline: pipeline-level vcs is null!
            return {"id": pipe_id, "vcs": None}
        if "/api/v2/project/gh/owner/repo/job/77" in url:
            return {
                "number": 77,
                "name": "android-build",
                "status": "success",
                "pipeline": {"id": pipe_id},
                "latest_workflow": {"id": wf_id},
            }
        if "/api/v1.1/project/gh/owner/repo/77" in url:
            return {
                "build_num": 77,
                "vcs_revision": head_sha,
                "status": "success",
                "workflows": {"job_name": "android-build", "workflow_id": wf_id},
            }
        if "/api/v2/project/gh/owner/repo/77/artifacts" in url:
            return {
                "items": [
                    {
                        "path": "app/build/outputs/apk/release/app-release.apk",
                        "url": "https://circleci.com/api/v2/artifacts/app-release.apk",
                        "size_bytes": 123456,
                    }
                ]
            }
        return None

    client = SubprocessGitGh(
        circle_token="tok",
        http_json_get=fake_http_json,
    )
    import nightshift.engine.vcs as vcs_mod
    orig_run = vcs_mod.run_subprocess_with_timeout
    vcs_mod.run_subprocess_with_timeout = lambda cmd, **kw: fake_gh(cmd, kw.get("timeout_seconds", 60))
    try:
        checks = client.get_commit_ci_checks("owner/repo", head_sha, branch="feat/p1")
    finally:
        vcs_mod.run_subprocess_with_timeout = orig_run

    assert len(checks) == 1
    chk = checks[0]
    assert chk.commit_sha == head_sha
    assert chk.status == "success"
    assert chk.build_id == "77"
    assert len(chk.artifacts) == 1
    assert chk.artifacts[0].path.endswith("app-release.apk")


# ---------------------------------------------------------------------------
# 5. Imported priority list seeded from ladders (Claude last) & unavailable checks
# ---------------------------------------------------------------------------

def test_import_seeds_priority_list_and_marks_unavailable_engines(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.set_setting("pipeline.freeOnlyMode", "1")
    creds = MemoryCredentialStore()
    bridge = BridgeAPI(store, EventBus(store), ModuleRegistry(), creds=creds)

    q = {
        "repo": "owner/drivelift-android",
        "name": "DriveLift",
        "failover_ladders": {
            "backend": ["claude", "agy", "gemini"],
            "ci_fix": ["deepseek", "codex"],
        },
        "phases": [{"id": "p1", "title": "P1", "branch": "feat/p1", "ladder": "backend"}],
    }
    res = bridge.call("plan.importQueue", {"queue": q})
    assert res["ok"] is True

    plist = bridge.call("settings.getPriorityList", {})
    assert plist["ok"] is True
    engines = [item["engine"] for item in plist["data"]]
    assert engines[-1] == "claude"
    assert set(engines) == {"agy", "gemini", "deepseek", "codex", "claude"}

    by_eng = {item["engine"]: item for item in plist["data"]}
    assert by_eng["deepseek"]["availability"] != "Ready"
    assert by_eng["claude"]["availability"] != "Ready"


# ---------------------------------------------------------------------------
# 6. Owner merge rule: require_codex_review (default ON) blocks even cosmetic
# ---------------------------------------------------------------------------

def test_require_codex_review_blocks_cosmetic_when_not_codex_or_missing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _, task, contract = _seed_project_and_task(
        store, tmp_path, risk_class="docs_format", require_codex_review=1
    )
    head_sha = "sha1111"
    obs_jobs = [CiJobCheck(name="desktop-backend", status="success", commit_sha=head_sha, build_id="10", duration_seconds=20.0)]
    ci_val = validate_ci_evidence(
        expected_commit_sha=head_sha,
        required_jobs=["desktop-backend"],
        observed_jobs=obs_jobs,
    )
    persist_ci_evidence(
        store,
        task_id=task["id"],
        contract_sha256=contract["contract_sha256"],
        commit_sha=head_sha,
        kind="ci_check",
        observed_jobs=obs_jobs,
        validation=ci_val,
    )
    gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=True)

    # Gemini review is NOT enough when require_codex_review=True
    auth_gem = gate.authorize_merge(
        task=task,
        contract=contract,
        head_sha=head_sha,
        expected_base_sha="base0",
        current_base_sha="base0",
        diff_entries=[],
        diff_content="",
        evidence_records=store.list_evidence_records(task["id"], commit_sha=head_sha),
        required_ci_jobs=["desktop-backend"],
        require_screenshot_evidence=False,
        require_review_evidence=False,
        reviewer_provider="google",
        reviewer_engine="gemini",
        reviewer_tier="standard",
    )
    assert auth_gem.allowed is False
    assert auth_gem.block_reason == "codex_review_required"

    # Codex review with 0 P1 on exact head -> allowed
    auth_codex = gate.authorize_merge(
        task=task,
        contract=contract,
        head_sha=head_sha,
        expected_base_sha="base0",
        current_base_sha="base0",
        diff_entries=[],
        diff_content="",
        evidence_records=store.list_evidence_records(task["id"], commit_sha=head_sha),
        required_ci_jobs=["desktop-backend"],
        require_screenshot_evidence=False,
        require_review_evidence=False,
        reviewer_provider="openai",
        reviewer_engine="codex",
        reviewer_tier="standard",
    )
    assert auth_codex.allowed is True


# ---------------------------------------------------------------------------
# 8. plan.importQueue maps legacy ladder names ('build' -> 'backend')
# ---------------------------------------------------------------------------

def test_import_queue_maps_legacy_build_ladder_to_backend(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    bridge = BridgeAPI(store, EventBus(store), ModuleRegistry())
    q = {
        "repo": "owner/drivelift-android",
        "failover_ladders": {"build": ["agy", "gemini"]},
        "phases": [{"id": "p1-shell-scaffold", "title": "Shell", "branch": "feat/p1", "ladder": "build"}],
    }
    res = bridge.call("plan.importQueue", {"queue": q})
    assert res["ok"] is True
    assert res["data"]["phases"][0]["ladder"] == "backend"
    val = bridge.call("plan.validate", {"projectId": "owner/drivelift-android", "planId": res["data"]["plan"]["id"]})
    assert val["data"]["valid"] is True


# ---------------------------------------------------------------------------
# 9. Codex runner argv & misconfigured runner skipped without burning attempt
# ---------------------------------------------------------------------------

def test_codex_runner_argv_omits_sandbox_flag() -> None:
    runner = CodexRunner()
    cmd = runner.build_command("Fix bug", model="o3-mini")
    assert "--sandbox" not in cmd
    assert cmd[:3] == ["codex", "exec", "--approve-for-me"]

    codex_default = next(e for e in SEED_ENGINES if e["id"] == "codex")
    assert "--sandbox" not in codex_default["command_template"]


# ---------------------------------------------------------------------------
# 10. Governance split safe path & .nightshift_prompt.md exclusion / diff guard
# ---------------------------------------------------------------------------

def test_diff_guard_rejects_nightshift_prompt_md(tmp_path: Path) -> None:
    findings = check_diff_guards(tmp_path, [".nightshift_prompt.md", "src/app.py"], "+hello")
    assert any(f.guard == "no_nightshift_artifacts" for f in findings)


def test_resolve_git_info_exclude_handles_worktree_commondir(tmp_path: Path) -> None:
    main_git = tmp_path / "repo" / ".git"
    wt_git = main_git / "worktrees" / "wt1"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt_dir = tmp_path / "wt1"
    wt_dir.mkdir()
    (wt_dir / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")

    exclude = resolve_git_info_exclude(wt_dir)
    assert exclude == (main_git / "info" / "exclude").resolve()


def test_governance_split_safe_path_with_equal_stricter_assertions_and_owner_approval(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _, task, contract = _seed_project_and_task(
        store,
        tmp_path,
        risk_class="business_logic",
        require_codex_review=0,
        allowed_globs=["backend/nightshift/**", "backend/tests/**"],
    )
    head_sha = "sha2222"
    obs_jobs = [CiJobCheck(name="desktop-backend", status="success", commit_sha=head_sha, build_id="11", duration_seconds=20.0)]
    ci_val = validate_ci_evidence(
        expected_commit_sha=head_sha,
        required_jobs=["desktop-backend"],
        observed_jobs=obs_jobs,
    )
    persist_ci_evidence(
        store,
        task_id=task["id"],
        contract_sha256=contract["contract_sha256"],
        commit_sha=head_sha,
        kind="ci_check",
        observed_jobs=obs_jobs,
        validation=ci_val,
    )

    from nightshift.engine.pipeline_contracts import DiffPathEntry

    entries = [
        DiffPathEntry(status="M", path="backend/nightshift/foo.py"),
        DiffPathEntry(status="M", path="backend/tests/test_foo.py"),
    ]
    snapshots = {
        "backend/tests/test_foo.py": (
            "def test_old():\n    assert 1 == 1\n",
            "def test_old():\n    assert 1 == 1\n    assert 2 == 2\n",
        )
    }

    gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=True)
    # Without owner approval -> awaiting_combined_governance_approval (not looping)
    auth_wait = gate.authorize_merge(
        task=task,
        contract=contract,
        head_sha=head_sha,
        expected_base_sha="base0",
        current_base_sha="base0",
        diff_entries=entries,
        diff_content="+assert 2 == 2",
        test_file_snapshots=snapshots,
        evidence_records=store.list_evidence_records(task["id"], commit_sha=head_sha),
        required_ci_jobs=["desktop-backend"],
        require_review_evidence=False,
        reviewer_provider="openai",
        reviewer_engine="codex",
        reviewer_tier="standard",
    )
    assert auth_wait.allowed is False
    assert auth_wait.block_reason == "awaiting_combined_governance_approval"

    # With owner approval on exact head_sha -> allowed!
    task_approved = dict(task)
    task_approved["combined_governance_approved_sha"] = head_sha
    task_approved["combined_governance_approved_reason"] = "Fixes stale assertion with stricter check"
    task_approved["owner_approved_sha"] = head_sha
    auth_ok = gate.authorize_merge(
        task=task_approved,
        contract=contract,
        head_sha=head_sha,
        expected_base_sha="base0",
        current_base_sha="base0",
        diff_entries=entries,
        diff_content="+assert 2 == 2",
        test_file_snapshots=snapshots,
        evidence_records=store.list_evidence_records(task["id"], commit_sha=head_sha),
        required_ci_jobs=["desktop-backend"],
        require_review_evidence=False,
        reviewer_provider="openai",
        reviewer_engine="codex",
        reviewer_tier="standard",
    )
    assert auth_ok.allowed is True


def test_ci_watch_defers_split_only_to_merge_gate_but_blocks_relaxation(tmp_path: Path) -> None:
    """CiPipelineManager must not fail with diff_guard_failed on split-only feature+test diffs,
    but must still block when assertion_relaxation is also present."""
    from nightshift.engine.ci import CiPipelineManager
    from nightshift.engine.pipeline_contracts import DiffPathEntry, canonical_contract_json, compute_contract_sha256
    from nightshift.engine.scheduler import Scheduler
    from nightshift.engine.vcs import FakeGitGh

    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    events = EventBus(store)
    proj = store.create_project("demo", "owner/repo", str(tmp_path), ladders={"ci_fix": ["codex"]})
    store.upsert_phase(id="p1", project_id=proj["id"], title="P1", prompt="x", branch="feat/p1", order_idx=0)
    contract_payload = {
        "task_id": "t-ci-split",
        "allowed_globs": ["backend/**"],
        "forbidden_globs": [".circleci/**"],
        "risk_class": "business_logic",
    }
    canon = canonical_contract_json(contract_payload)
    c_sha = compute_contract_sha256(canon)
    store.create_frozen_contract(
        contract_id="c-ci-split",
        task_id="t-ci-split",
        project_id=proj["id"],
        contract_sha256=c_sha,
        canonical_json=canon,
        allowed_globs_json='["backend/**"]',
        forbidden_globs_json='[".circleci/**"]',
    )
    task = store.create_task(
        task_id="t-ci-split",
        project_id=proj["id"],
        phase_id="p1",
        contract_id="c-ci-split",
        contract_sha256=c_sha,
        status="VERIFYING",
    )

    vcs = FakeGitGh()
    vcs.pr_checks["owner/repo#1"] = [
        {"name": "ci/circleci: desktop-backend", "bucket": "pass", "state": "SUCCESS", "link": ""}
    ]
    vcs.pr_details["owner/repo#1"] = {"headRefOid": "head111", "baseRefOid": "base000", "state": "OPEN"}
    vcs.changed_entries[str(tmp_path)] = [
        DiffPathEntry(status="M", path="backend/nightshift/foo.py"),
        DiffPathEntry(status="M", path="backend/tests/test_foo.py"),
    ]
    vcs.changed_files[str(tmp_path)] = ["backend/nightshift/foo.py", "backend/tests/test_foo.py"]
    vcs.diffs[str(tmp_path)] = "+assert 2 == 2"
    vcs.test_snapshots[str(tmp_path)] = {
        "backend/tests/test_foo.py": (
            "def test_old():\n    assert 1 == 1\n",
            "def test_old():\n    assert 1 == 1\n    assert 2 == 2\n",
        )
    }

    ledger = QuotaLedger(store)
    scheduler = Scheduler(store, ledger, events, runners={"codex": FakeRunner("codex", vcs)})
    ci_mgr = CiPipelineManager(store, vcs, scheduler, events)

    res_split = ci_mgr.watch_and_fix(
        repo="owner/repo",
        branch="feat/p1",
        pr_number=1,
        worktree_dir=tmp_path,
        phase_id="p1",
        ladder="ci_fix",
        max_rounds=1,
        poll_interval=0.0,
        task_id=task["id"],
        task_contract=store.get_frozen_contract("c-ci-split"),
        require_codex_review=False,
    )
    assert res_split.block_reason != "diff_guard_failed"

    # Now introduce an assertion relaxation alongside the split -> MUST block with diff_guard_failed
    vcs.test_snapshots[str(tmp_path)] = {
        "backend/tests/test_foo.py": (
            "def test_old():\n    assert 1 == 1\n",
            "def test_old():\n    pass\n",
        )
    }
    res_relax = ci_mgr.watch_and_fix(
        repo="owner/repo",
        branch="feat/p1",
        pr_number=1,
        worktree_dir=tmp_path,
        phase_id="p1",
        ladder="ci_fix",
        max_rounds=1,
        poll_interval=0.0,
        task_id=task["id"],
        task_contract=store.get_frozen_contract("c-ci-split"),
        require_codex_review=False,
    )
    assert res_relax.passed is False
    assert res_relax.block_reason == "diff_guard_failed"

