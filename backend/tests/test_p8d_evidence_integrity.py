"""Phase 8d tests: Trustworthy verification & evidence integrity (Amendment F).

Covers docs/AGENT_PIPELINE.md sections 9.1 and 12.1 Phase 8d requirements:
- Exact-SHA and race-safe expected_base_sha verification (pre-merge)
- Post-merge merge_sha CI verification before release
- Real job duration, skipped/compat-mode rejection, and non-empty artifact enforcement
- Verified-build screenshot-manifest.json validation
- PR-body-is-not-evidence rule across MergeGate, TaskOrchestrator, and EngineStateMachine
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.pipeline_contracts import MergeGate
from nightshift.engine.pipeline_evidence import (
    CiArtifact,
    CiJobCheck,
    is_untrusted_evidence_source,
    persist_ci_evidence,
    render_mechanical_pr_body,
    validate_ci_evidence,
    validate_evidence_record_trust,
    validate_screenshot_manifest,
    write_screenshot_manifest,
)
from nightshift.engine.pipeline_orchestrator import InvalidTransitionError, TaskOrchestrator
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
from nightshift.engine.vcs import FakeGitGh
from nightshift.events import EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.fake import FakeRunner
from nightshift.store import Store

_MINIMAL_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16


def _ok_login_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    return (0, '{"loggedIn": true}\nLogged in to github.com\nOK', "", False)


def _make_valid_jobs(
    commit_sha: str,
    *,
    branch: str = "feat/p8d",
    build_prefix: str = "build",
) -> list[CiJobCheck]:
    return [
        CiJobCheck(
            name="desktop-gates",
            status="success",
            commit_sha=commit_sha,
            build_id=f"{build_prefix}-1",
            duration_seconds=8.5,
            is_gate_job=True,
            branch=branch,
        ),
        CiJobCheck(
            name="desktop-backend",
            status="success",
            commit_sha=commit_sha,
            build_id=f"{build_prefix}-2",
            duration_seconds=34.0,
            branch=branch,
        ),
        CiJobCheck(
            name="desktop-frontend",
            status="success",
            commit_sha=commit_sha,
            build_id=f"{build_prefix}-3",
            duration_seconds=62.0,
            artifacts=[
                CiArtifact(
                    path="ui-screenshots/mission-control-idle-1440x900.png",
                    size_bytes=4096,
                    build_id=f"{build_prefix}-3",
                ),
                CiArtifact(
                    path="ui-screenshots/screenshot-manifest.json",
                    size_bytes=512,
                    build_id=f"{build_prefix}-3",
                ),
            ],
            branch=branch,
        ),
        CiJobCheck(
            name="desktop-package",
            status="success",
            commit_sha=commit_sha,
            build_id=f"{build_prefix}-4",
            duration_seconds=120.0,
            artifacts=[
                CiArtifact(
                    path="nightshift-windows/NightShift-windows.zip",
                    size_bytes=12_345_678,
                    build_id=f"{build_prefix}-4",
                ),
            ],
            branch=branch,
        ),
    ]


def _make_valid_manifest(
    screenshots_dir: Path,
    *,
    commit_sha: str,
    build_id: str = "build-3",
    renderer: str = "playwright-mockbridge",
) -> Path:
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for vp, scale in (("1440x900", "100%"), ("1024x700", "100%"), ("1440x900-150pct", "150%")):
        fname = f"mission-control-idle-{vp}.png"
        png_path = screenshots_dir / fname
        png_path.write_bytes(_MINIMAL_PNG + vp.encode("utf-8"))
        sha = hashlib.sha256(png_path.read_bytes()).hexdigest()
        entries.append(
            {
                "file": fname,
                "viewport_or_device": vp,
                "scale": scale,
                "axe_violations": 0,
                "overflow": False,
                "sha256": sha,
            }
        )
    return write_screenshot_manifest(
        screenshots_dir / "screenshot-manifest.json",
        build_id=build_id,
        commit_sha=commit_sha,
        renderer=renderer,
        screenshots=entries,
    )


def _setup_task(
    store: Store,
    *,
    task_id: str = "t-8d-01",
    branch: str = "feat/p8d-test",
    risk_class: str = "business_logic",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if store.get_project("proj") is None:
        store.create_project("proj", "Proj", "owner/repo", "/tmp/repo")
    if store.get_plan("plan-1") is None:
        store.create_plan("plan-1", "proj", "Plan 1")
    if store.get_phase("p1") is None:
        store.upsert_phase("p1", "plan-1", "Phase 1", branch)
    return store.create_task_with_contract(
        task_id=task_id,
        phase_id="p1",
        plan_id="plan-1",
        title="Evidence integrity task",
        branch=branch,
        risk_class=risk_class,
        allowed_globs=["src/**"],
    )


# ---------------------------------------------------------------------------
# 1. Exact-SHA, base-SHA, duration, skipped/compat-mode, and artifact checks
# ---------------------------------------------------------------------------


def test_validate_ci_evidence_accepts_valid_exact_sha_run() -> None:
    jobs = _make_valid_jobs("sha_head_001")
    res = validate_ci_evidence(
        expected_commit_sha="sha_head_001",
        required_jobs=["desktop-gates", "desktop-backend", "desktop-frontend", "desktop-package"],
        observed_jobs=jobs,
        required_artifacts_by_job={
            "desktop-frontend": ["ui-screenshots"],
            "desktop-package": ["nightshift-windows/NightShift-windows.zip"],
        },
        expected_base_sha="sha_main_001",
        current_base_sha="sha_main_001",
        require_base_sha_match=True,
    )
    assert res.verified is True
    assert res.rejection_reason == ""
    assert len(res.details) == 4


def test_validate_ci_evidence_rejects_zero_duration_skipped_and_compat_mode() -> None:
    # Zero duration
    jobs_zero = _make_valid_jobs("sha_01")
    jobs_zero[1] = CiJobCheck(
        name="desktop-backend",
        status="success",
        commit_sha="sha_01",
        build_id="build-2",
        duration_seconds=0.0,
    )
    res_zero = validate_ci_evidence(
        expected_commit_sha="sha_01",
        required_jobs=["desktop-gates", "desktop-backend"],
        observed_jobs=jobs_zero,
    )
    assert res_zero.verified is False
    assert res_zero.rejection_reason == "suspicious_zero_duration_check"

    # Sub-threshold gate (< 5s) and build (< 15s)
    jobs_fast_gate = [
        CiJobCheck(
            name="desktop-gates",
            status="success",
            commit_sha="sha_01",
            build_id="b1",
            duration_seconds=2.0,
            is_gate_job=True,
        ),
    ]
    res_fast = validate_ci_evidence(
        expected_commit_sha="sha_01",
        required_jobs=["desktop-gates"],
        observed_jobs=jobs_fast_gate,
    )
    assert res_fast.verified is False
    assert res_fast.rejection_reason == "suspicious_zero_duration_check"

    # Skipped job
    jobs_skip = [
        CiJobCheck(
            name="desktop-backend",
            status="skipped",
            commit_sha="sha_01",
            build_id="b2",
            duration_seconds=25.0,
        ),
    ]
    res_skip = validate_ci_evidence(
        expected_commit_sha="sha_01",
        required_jobs=["desktop-backend"],
        observed_jobs=jobs_skip,
    )
    assert res_skip.verified is False
    assert res_skip.rejection_reason == "suspicious_zero_duration_check"

    # Compat-mode passthrough green
    jobs_compat = [
        CiJobCheck(
            name="desktop-package",
            status="success",
            commit_sha="sha_01",
            build_id="b4",
            duration_seconds=45.0,
            compat_mode=True,
        ),
    ]
    res_compat = validate_ci_evidence(
        expected_commit_sha="sha_01",
        required_jobs=["desktop-package"],
        observed_jobs=jobs_compat,
    )
    assert res_compat.verified is False
    assert res_compat.rejection_reason == "suspicious_zero_duration_check"


def test_validate_ci_evidence_rejects_stale_sha_drifted_base_and_empty_required_jobs() -> None:
    # Stale commit SHA
    jobs = _make_valid_jobs("sha_old_999")
    res_stale = validate_ci_evidence(
        expected_commit_sha="sha_new_001",
        required_jobs=["desktop-gates", "desktop-backend"],
        observed_jobs=jobs,
    )
    assert res_stale.verified is False
    assert res_stale.rejection_reason == "stale_commit_sha"

    # Drifted base branch SHA
    res_drift = validate_ci_evidence(
        expected_commit_sha="sha_old_999",
        required_jobs=["desktop-gates"],
        observed_jobs=jobs,
        expected_base_sha="sha_main_001",
        current_base_sha="sha_main_002",
    )
    assert res_drift.verified is False
    assert res_drift.rejection_reason == "base_branch_drifted"

    # Missing base SHA when strict match requested
    res_missing_base = validate_ci_evidence(
        expected_commit_sha="sha_old_999",
        required_jobs=["desktop-gates"],
        observed_jobs=jobs,
        require_base_sha_match=True,
    )
    assert res_missing_base.verified is False
    assert res_missing_base.rejection_reason == "missing_base_sha"

    # Empty required_jobs fails closed
    res_empty = validate_ci_evidence(
        expected_commit_sha="sha_old_999",
        required_jobs=[],
        observed_jobs=jobs,
    )
    assert res_empty.verified is False
    assert res_empty.rejection_reason == "no_required_jobs_configured"


def test_validate_ci_evidence_enforces_non_empty_build_bound_artifacts() -> None:
    # Missing required artifact
    job_no_art = CiJobCheck(
        name="desktop-package",
        status="success",
        commit_sha="sha_a1",
        build_id="b-10",
        duration_seconds=90.0,
        artifacts=[],
    )
    res_missing = validate_ci_evidence(
        expected_commit_sha="sha_a1",
        required_jobs=["desktop-package"],
        observed_jobs=[job_no_art],
        required_artifacts_by_job={"desktop-package": ["nightshift-windows/NightShift-windows.zip"]},
    )
    assert res_missing.verified is False
    assert res_missing.rejection_reason == "missing_required_artifact"

    # 0-byte artifact rejected
    job_zero_bytes = CiJobCheck(
        name="desktop-package",
        status="success",
        commit_sha="sha_a1",
        build_id="b-10",
        duration_seconds=90.0,
        artifacts=[CiArtifact(path="nightshift-windows/NightShift-windows.zip", size_bytes=0, build_id="b-10")],
    )
    res_empty_art = validate_ci_evidence(
        expected_commit_sha="sha_a1",
        required_jobs=["desktop-package"],
        observed_jobs=[job_zero_bytes],
        required_artifacts_by_job={"desktop-package": ["nightshift-windows/NightShift-windows.zip"]},
    )
    assert res_empty_art.verified is False
    assert res_empty_art.rejection_reason == "missing_required_artifact"
    assert "empty (0 bytes)" in res_empty_art.details[0]

    # Artifact from a different build_id rejected
    job_wrong_build = CiJobCheck(
        name="desktop-package",
        status="success",
        commit_sha="sha_a1",
        build_id="b-10",
        duration_seconds=90.0,
        artifacts=[CiArtifact(path="nightshift-windows/NightShift-windows.zip", size_bytes=1024, build_id="b-999")],
    )
    res_wrong_build = validate_ci_evidence(
        expected_commit_sha="sha_a1",
        required_jobs=["desktop-package"],
        observed_jobs=[job_wrong_build],
        required_artifacts_by_job={"desktop-package": ["nightshift-windows/NightShift-windows.zip"]},
    )
    assert res_wrong_build.verified is False
    assert res_wrong_build.rejection_reason == "missing_required_artifact"
    assert "does not belong to build" in res_wrong_build.details[0]


# ---------------------------------------------------------------------------
# 2. Verified-build screenshot manifest validation
# ---------------------------------------------------------------------------


def test_screenshot_manifest_validation_happy_and_tamper_cases(tmp_path: Path) -> None:
    shots_dir = tmp_path / "screenshots"
    manifest_path = _make_valid_manifest(shots_dir, commit_sha="sha_ui_123", build_id="ci-42")

    # Valid manifest passes
    ok = validate_screenshot_manifest(
        manifest_path,
        expected_commit_sha="sha_ui_123",
        expected_build_id="ci-42",
        required_viewports=["1440x900", "1024x700", "1440x900-150pct"],
    )
    assert ok.verified is True
    assert len(ok.details) == 3

    # Mismatched commit SHA rejected
    bad_sha = validate_screenshot_manifest(manifest_path, expected_commit_sha="sha_other")
    assert bad_sha.verified is False
    assert bad_sha.rejection_reason == "screenshot_commit_sha_mismatch"

    # Mismatched build_id rejected
    bad_build = validate_screenshot_manifest(
        manifest_path,
        expected_commit_sha="sha_ui_123",
        expected_build_id="ci-999",
    )
    assert bad_build.verified is False
    assert bad_build.rejection_reason == "screenshot_build_id_mismatch"

    # Missing required viewport rejected
    bad_vp = validate_screenshot_manifest(
        manifest_path,
        expected_commit_sha="sha_ui_123",
        required_viewports=["1440x900", "390x844"],
    )
    assert bad_vp.verified is False
    assert bad_vp.rejection_reason == "missing_required_viewports"

    # Tampered PNG content -> SHA-256 mismatch
    (shots_dir / "mission-control-idle-1440x900.png").write_bytes(_MINIMAL_PNG + b"tampered")
    bad_hash = validate_screenshot_manifest(manifest_path, expected_commit_sha="sha_ui_123")
    assert bad_hash.verified is False
    assert bad_hash.rejection_reason == "screenshot_sha256_mismatch"


def test_screenshot_manifest_rejects_uncontrolled_renderer_axe_overflow_and_traversal(tmp_path: Path) -> None:
    shots_dir = tmp_path / "shots"
    shots_dir.mkdir()
    png = shots_dir / "page.png"
    png.write_bytes(_MINIMAL_PNG + b"ok")
    sha = hashlib.sha256(png.read_bytes()).hexdigest()

    # Uncontrolled renderer
    m1 = write_screenshot_manifest(
        shots_dir / "m1.json",
        build_id="b1",
        commit_sha="sha1",
        renderer="manual-upload",
        screenshots=[
            {"file": "page.png", "viewport_or_device": "1440x900", "scale": "100%", "axe_violations": 0, "overflow": False, "sha256": sha}
        ],
    )
    assert validate_screenshot_manifest(m1, expected_commit_sha="sha1").rejection_reason == "uncontrolled_screenshot_renderer"

    # Axe violations > 0
    m2 = write_screenshot_manifest(
        shots_dir / "m2.json",
        build_id="b1",
        commit_sha="sha1",
        screenshots=[
            {"file": "page.png", "viewport_or_device": "1440x900", "scale": "100%", "axe_violations": 2, "overflow": False, "sha256": sha}
        ],
    )
    assert validate_screenshot_manifest(m2, expected_commit_sha="sha1").rejection_reason == "axe_violations_present"

    # Horizontal overflow
    m3 = write_screenshot_manifest(
        shots_dir / "m3.json",
        build_id="b1",
        commit_sha="sha1",
        screenshots=[
            {"file": "page.png", "viewport_or_device": "1440x900", "scale": "100%", "axe_violations": 0, "overflow": True, "sha256": sha}
        ],
    )
    assert validate_screenshot_manifest(m3, expected_commit_sha="sha1").rejection_reason == "horizontal_overflow_detected"

    # Path traversal
    outside = tmp_path / "secret.png"
    outside.write_bytes(_MINIMAL_PNG)
    m4 = write_screenshot_manifest(
        shots_dir / "m4.json",
        build_id="b1",
        commit_sha="sha1",
        screenshots=[
            {
                "file": "../secret.png",
                "viewport_or_device": "1440x900",
                "scale": "100%",
                "axe_violations": 0,
                "overflow": False,
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        ],
    )
    assert validate_screenshot_manifest(m4, expected_commit_sha="sha1").rejection_reason == "screenshot_path_traversal"


# ---------------------------------------------------------------------------
# 3. PR-body-is-not-evidence rule (MergeGate, TaskOrchestrator, mechanical PR body)
# ---------------------------------------------------------------------------


def test_pr_body_cannot_satisfy_merge_gate_or_resume(tmp_path: Path) -> None:
    assert is_untrusted_evidence_source("pr_body") is True
    assert is_untrusted_evidence_source("pr_description:checks") is True
    assert is_untrusted_evidence_source("self_report") is True
    assert is_untrusted_evidence_source("circleci:desktop-backend") is False

    with Store(tmp_path / "pr_body.db") as store:
        store.migrate()
        task, contract = _setup_task(store, task_id="t-prbody", branch="feat/prbody")
        store.record_task_owner_approval("t-prbody", "owner", "sha_pr_1")
        task = store.get_task("t-prbody")
        assert task is not None

        # Even if a row is inserted claiming CI passed from pr_body, MergeGate must reject it!
        pr_body_record = {
            "kind": "ci_check",
            "source": "pr_body:claimed_green",
            "commit_sha": "sha_pr_1",
            "contract_sha256": contract["contract_sha256"],
            "duration_seconds": 60.0,
            "verified": 1,
            "metadata_json": json.dumps({"job_name": "desktop-backend", "from_pr_body": True}),
        }
        trusted, _ = validate_evidence_record_trust(pr_body_record)
        assert trusted is False

        gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=False)
        auth = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_pr_1",
            evidence_records=[pr_body_record],
            required_ci_jobs=["desktop-backend"],
        )
        assert auth.allowed is False
        assert auth.block_reason == "missing_verified_evidence"

        # TaskOrchestrator.resume_from_blocked must also reject pr_body evidence!
        orch = TaskOrchestrator(store)
        orch.transition("t-prbody", "RUNNING", idempotency_key="r1", reason="start")
        orch.transition("t-prbody", "VERIFYING", idempotency_key="r2", reason="pr", updates={"head_sha": "sha_pr_1", "pr_number": 5})
        store.record_evidence(
            task_id="t-prbody",
            contract_sha256=contract["contract_sha256"],
            commit_sha="sha_pr_1",
            kind="ci_check",
            source="pr_body",
            duration_seconds=40.0,
            verified=True,
        )
        orch.transition("t-prbody", "REVIEWING", idempotency_key="r3", reason="rev")
        orch.transition("t-prbody", "BLOCKED", idempotency_key="r4", reason="awaiting_owner_approval")
        with pytest.raises(InvalidTransitionError, match="verified CI evidence missing"):
            orch.resume_from_blocked("t-prbody", current_head_sha="sha_pr_1", owner_approved_by="owner")

        # Mechanical PR body renders real verified data deterministically
        body = render_mechanical_pr_body(
            task=task,
            contract=contract,
            changed_files=["src/app.py"],
            evidence_records=[pr_body_record],
            screenshots=["mission-control-idle-1440x900.png"],
        )
        assert "## Task: Evidence integrity task" in body
        assert "`src/app.py`" in body
        assert "mission-control-idle-1440x900.png" in body


def test_merge_gate_requires_all_configured_jobs_and_screenshots_when_enabled(tmp_path: Path) -> None:
    with Store(tmp_path / "gate_jobs.db") as store:
        store.migrate()
        task, contract = _setup_task(store, task_id="t-jobs", branch="feat/jobs")
        store.record_task_owner_approval("t-jobs", "owner", "sha_j1")
        task = store.get_task("t-jobs")
        assert task is not None

        gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=False)
        partial_ci = [
            {
                "kind": "ci_check",
                "source": "circleci:desktop-gates",
                "commit_sha": "sha_j1",
                "contract_sha256": contract["contract_sha256"],
                "duration_seconds": 9.0,
                "verified": 1,
                "metadata_json": json.dumps({"job_name": "desktop-gates"}),
            }
        ]
        res_missing_job = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_j1",
            evidence_records=partial_ci,
            required_ci_jobs=["desktop-gates", "desktop-backend"],
        )
        assert res_missing_job.allowed is False
        assert res_missing_job.block_reason == "missing_required_job_evidence"

        full_ci = partial_ci + [
            {
                "kind": "ci_check",
                "source": "circleci:desktop-backend",
                "commit_sha": "sha_j1",
                "contract_sha256": contract["contract_sha256"],
                "duration_seconds": 35.0,
                "verified": 1,
                "metadata_json": json.dumps({"job_name": "desktop-backend"}),
            }
        ]
        res_missing_shots = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_j1",
            evidence_records=full_ci,
            required_ci_jobs=["desktop-gates", "desktop-backend"],
            require_screenshot_evidence=True,
        )
        assert res_missing_shots.allowed is False
        assert res_missing_shots.block_reason == "missing_verified_screenshots"

        with_shots = full_ci + [
            {
                "kind": "screenshot",
                "source": "screenshot-manifest:playwright-mockbridge",
                "commit_sha": "sha_j1",
                "contract_sha256": contract["contract_sha256"],
                "duration_seconds": 0.0,
                "verified": 1,
                "metadata_json": "{}",
            }
        ]
        res_ok = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_j1",
            evidence_records=with_shots,
            required_ci_jobs=["desktop-gates", "desktop-backend"],
            require_screenshot_evidence=True,
        )
        assert res_ok.allowed is True


# ---------------------------------------------------------------------------
# 4. End-to-end EngineStateMachine integration tests (pre-merge & post-merge)
# ---------------------------------------------------------------------------


def _build_engine(
    tmp_path: Path,
    vcs: FakeGitGh,
    *,
    autonomous_merge: bool = True,
) -> tuple[Store, EngineStateMachine]:
    store = Store(tmp_path / "engine_p8d.db")
    store.migrate()
    events = EventBus(store)
    quota = QuotaLedger(store)
    logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
    runners = {
        "agy": FakeRunner.success("agy", stdout="Implemented\n"),
        "codex": FakeRunner.success("codex", stdout="LGTM. Clean change.\n"),
    }
    engine = EngineStateMachine(
        store=store,
        event_bus=events,
        quota_ledger=quota,
        vcs=vcs,
        runners=runners,
        login_checker=logins,
        merge_gate=MergeGate(autonomous_merge_enabled=autonomous_merge, require_independent_reviewer=False),
    )
    return store, engine


@pytest.mark.asyncio
async def test_e2e_blocks_merge_on_zero_duration_ci_check(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    pr = vcs.add_pr("feat/zero-dur", number=801, head_sha="sha_zero_1")
    jobs = _make_valid_jobs("sha_zero_1", branch="feat/zero-dur")
    jobs[1] = CiJobCheck(
        name="desktop-backend",
        status="success",
        commit_sha="sha_zero_1",
        build_id="build-2",
        duration_seconds=0.0,
        branch="feat/zero-dur",
    )
    vcs.commit_ci_checks["sha_zero_1"] = jobs

    store, engine = _build_engine(tmp_path, vcs)
    try:
        project = ProjectConfig(
            id="proj-8d",
            name="NightShift",
            repo="owner/nightshift",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "out",
            required_ci_jobs=["desktop-gates", "desktop-backend", "desktop-frontend", "desktop-package"],
        )
        phase = PhaseDefinition(
            id="p8d-zero",
            title="Zero-duration test",
            prompt="Implement feature",
            branch="feat/zero-dur",
            auto_merge=True,
            release_after_merge=False,
        )
        rows = await engine.execute_plan(project, [phase], plan_id="plan-zero")
        assert rows[0]["status"] == "PARKED"
        assert "suspicious_zero_duration_check" in rows[0]["detail"]
        assert "feat/zero-dur" not in vcs.merged_prs
        assert pr["number"] == 801

        task = store.get_task("task-p8d-zero")
        assert task is not None
        assert task["status"] == "BLOCKED"
        assert task["blocked_from_status"] == "VERIFYING"
        assert task["block_reason"] == "suspicious_zero_duration_check"

        ev = store.list_evidence_records("task-p8d-zero", commit_sha="sha_zero_1")
        assert any(r["rejection_reason"] == "suspicious_zero_duration_check" and r["verified"] == 0 for r in ev)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_e2e_blocks_merge_on_missing_required_artifact(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    vcs.add_pr("feat/missing-art", number=802, head_sha="sha_art_1")
    jobs = _make_valid_jobs("sha_art_1", branch="feat/missing-art")
    jobs[3] = CiJobCheck(
        name="desktop-package",
        status="success",
        commit_sha="sha_art_1",
        build_id="build-4",
        duration_seconds=110.0,
        artifacts=[],
        branch="feat/missing-art",
    )
    vcs.commit_ci_checks["sha_art_1"] = jobs

    store, engine = _build_engine(tmp_path, vcs)
    try:
        project = ProjectConfig(
            id="proj-8d-art",
            name="NightShift",
            repo="owner/nightshift",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "out",
            required_ci_jobs=["desktop-gates", "desktop-backend", "desktop-frontend", "desktop-package"],
            required_artifacts_by_job={"desktop-package": ["nightshift-windows/NightShift-windows.zip"]},
        )
        phase = PhaseDefinition(
            id="p8d-art",
            title="Missing artifact test",
            prompt="Implement feature",
            branch="feat/missing-art",
            auto_merge=True,
            release_after_merge=False,
        )
        rows = await engine.execute_plan(project, [phase], plan_id="plan-art")
        assert rows[0]["status"] == "PARKED"
        assert "missing_required_artifact" in rows[0]["detail"]
        assert "feat/missing-art" not in vcs.merged_prs
    finally:
        store.close()


@pytest.mark.asyncio
async def test_e2e_blocks_release_when_post_merge_ci_is_stale_or_zero_duration(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    vcs.add_pr("feat/post-merge-bad", number=803, head_sha="sha_head_803")
    vcs.commit_ci_checks["sha_head_803"] = _make_valid_jobs("sha_head_803", branch="feat/post-merge-bad")
    # FakeGitGh.merge_pr produces mergeCommit.oid = "sha_merge_123".
    # Simulate post-merge CI returning stale PR head_sha instead of actual merge_sha!
    vcs.commit_ci_checks["sha_merge_123"] = _make_valid_jobs("sha_head_803", branch="main")

    store, engine = _build_engine(tmp_path, vcs)
    try:
        project = ProjectConfig(
            id="proj-8d-post",
            name="NightShift",
            repo="owner/nightshift",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "out",
            artifact_glob="*.zip",
            required_ci_jobs=["desktop-gates", "desktop-backend", "desktop-frontend", "desktop-package"],
        )
        phase = PhaseDefinition(
            id="p8d-post",
            title="Post-merge stale SHA test",
            prompt="Implement feature",
            branch="feat/post-merge-bad",
            auto_merge=True,
            release_after_merge=True,
        )
        rows = await engine.execute_plan(project, [phase], plan_id="plan-post")
        assert rows[0]["status"] == "PARKED"
        assert "stale_commit_sha" in rows[0]["detail"]
        # PR merged, but ZERO releases were published because post-merge merge_sha check failed!
        assert "feat/post-merge-bad" in vcs.merged_prs
        assert len(vcs.releases) == 0

        task = store.get_task("task-p8d-post")
        assert task is not None
        assert task["status"] == "BLOCKED"
        assert task["blocked_from_status"] == "MERGED"
        assert task["block_reason"] == "stale_commit_sha"

        post_ev = [
            r for r in store.list_evidence_for_task("task-p8d-post") if r["kind"] == "post_merge_ci"
        ]
        assert post_ev
        assert all(r["verified"] == 0 for r in post_ev)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_e2e_happy_path_with_screenshot_manifest_and_post_merge_evidence(tmp_path: Path) -> None:
    vcs = FakeGitGh()
    vcs.add_pr("feat/p8d-happy", number=804, head_sha="sha_head_804")
    vcs.commit_ci_checks["sha_head_804"] = _make_valid_jobs(
        "sha_head_804",
        branch="feat/p8d-happy",
        build_prefix="pre",
    )
    vcs.commit_ci_checks["sha_merge_123"] = _make_valid_jobs(
        "sha_merge_123",
        branch="main",
        build_prefix="post",
    )
    manifest_path = _make_valid_manifest(
        tmp_path / "ui-screenshots",
        commit_sha="sha_head_804",
        build_id="pre-3",
    )

    store, engine = _build_engine(tmp_path, vcs)
    try:
        project = ProjectConfig(
            id="proj-8d-happy",
            name="NightShift",
            repo="owner/nightshift",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "out",
            artifact_glob="*.zip",
            required_ci_jobs=["desktop-gates", "desktop-backend", "desktop-frontend", "desktop-package"],
            required_artifacts_by_job={
                "desktop-frontend": ["ui-screenshots"],
                "desktop-package": ["nightshift-windows/NightShift-windows.zip"],
            },
            screenshot_manifest_path=str(manifest_path),
        )
        phase = PhaseDefinition(
            id="p8d-happy",
            title="Full evidence integrity happy path",
            prompt="Implement feature",
            branch="feat/p8d-happy",
            auto_merge=True,
            release_after_merge=True,
        )
        rows = await engine.execute_plan(project, [phase], plan_id="plan-happy")
        assert rows[0]["status"] == "COMPLETED"
        assert "feat/p8d-happy" in vcs.merged_prs
        assert len(vcs.releases) == 1

        ev_all = store.list_evidence_for_task("task-p8d-happy")
        assert sum(1 for r in ev_all if r["kind"] == "ci_check" and r["verified"] == 1) == 4
        assert sum(1 for r in ev_all if r["kind"] == "screenshot" and r["verified"] == 1) == 1
        assert sum(1 for r in ev_all if r["kind"] == "post_merge_ci" and r["verified"] == 1) == 4
    finally:
        store.close()


def test_persist_ci_evidence_records_rejected_audit_row_when_no_jobs(tmp_path: Path) -> None:
    with Store(tmp_path / "persist.db") as store:
        store.migrate()
        task, contract = _setup_task(store, task_id="t-empty-jobs", branch="feat/empty-jobs")
        res = validate_ci_evidence(
            expected_commit_sha="sha_e1",
            required_jobs=["desktop-gates"],
            observed_jobs=[],
        )
        assert res.verified is False
        ids = persist_ci_evidence(
            store,
            task_id=task["id"],
            contract_sha256=contract["contract_sha256"],
            commit_sha="sha_e1",
            kind="ci_check",
            observed_jobs=[],
            validation=res,
        )
        assert len(ids) == 1
        rows = store.list_evidence_records(task["id"], commit_sha="sha_e1")
        assert len(rows) == 1
        assert rows[0]["verified"] == 0
        assert rows[0]["rejection_reason"] == "missing_required_job"


# ---------------------------------------------------------------------------
# 5. SubprocessGitGh CircleCI / GitHub Actions adapter & project persistence
# ---------------------------------------------------------------------------


def test_subprocess_git_gh_circleci_status_resolves_build_num_duration_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nightshift.engine.vcs import SubprocessGitGh

    wf_id = "daf4a8fa-1bb8-4aab-a88d-3f56bb91f7d0"
    fe_uuid = "4cd387cd-f053-4ef0-bb63-2d3160104ae4"
    pkg_uuid = "94bb1474-ba72-4c46-b510-95c8b44a814c"
    slug = "circleci/org123/proj456"

    def fake_run(cmd: list[str], cwd: Any = None, timeout_seconds: int = 60) -> tuple[int, str, str, bool]:
        if cmd[:2] == ["gh", "api"] and "/check-runs" in cmd[2]:
            return (0, json.dumps({"total_count": 0, "check_runs": []}), "", False)
        if cmd[:2] == ["gh", "api"] and "/status" in cmd[2]:
            return (
                0,
                json.dumps(
                    {
                        "sha": "sha_circle_1",
                        "state": "success",
                        "statuses": [
                            {
                                "id": 60079844076,
                                "context": "ci/circleci: desktop-frontend",
                                "state": "success",
                                "created_at": "2026-11-27T21:42:36Z",
                                "updated_at": "2026-11-27T21:42:36Z",
                                "target_url": f"https://app.circleci.com/workflow/{wf_id}/job/{fe_uuid}",
                            },
                            {
                                "id": 60079965022,
                                "context": "ci/circleci: desktop-package",
                                "state": "success",
                                "created_at": "2026-11-27T21:45:24Z",
                                "updated_at": "2026-11-27T21:45:24Z",
                                "target_url": f"https://app.circleci.com/workflow/{wf_id}/job/{pkg_uuid}",
                            },
                        ],
                    }
                ),
                "",
                False,
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr("nightshift.engine.vcs.run_subprocess_with_timeout", fake_run)

    http_calls: list[str] = []

    def fake_http_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any] | None:
        http_calls.append(url)
        if url == f"https://circleci.com/api/v2/workflow/{wf_id}/job":
            return {
                "items": [
                    {
                        "id": fe_uuid,
                        "job_number": 142,
                        "name": "desktop-frontend",
                        "project_slug": slug,
                        "status": "success",
                        "started_at": "2026-11-27T21:34:43.507Z",
                        "stopped_at": "2026-11-27T21:42:36.241Z",
                    },
                    {
                        "id": pkg_uuid,
                        "job_number": 145,
                        "name": "desktop-package",
                        "project_slug": slug,
                        "status": "success",
                        "started_at": "2026-11-27T21:42:40.440Z",
                        "stopped_at": "2026-11-27T21:45:23.693Z",
                    },
                ],
                "next_page_token": None,
            }
        if url == f"https://circleci.com/api/v2/project/{slug}/142/artifacts":
            return {
                "items": [
                    {
                        "path": "ui-screenshots/screenshot-manifest.json",
                        "url": "https://output.circle-artifacts.com/output/job/fe/artifacts/0/ui-screenshots/screenshot-manifest.json",
                    },
                    {
                        "path": "ui-screenshots/mission-control-idle-1440x900.png",
                        "url": "https://output.circle-artifacts.com/output/job/fe/artifacts/0/ui-screenshots/mission-control-idle-1440x900.png",
                    },
                ],
                "next_page_token": None,
            }
        if url == f"https://circleci.com/api/v2/project/{slug}/145/artifacts":
            return {
                "items": [
                    {
                        "path": "nightshift-windows/NightShift-windows.zip",
                        "url": "https://output.circle-artifacts.com/output/job/pkg/artifacts/0/nightshift-windows/NightShift-windows.zip",
                    }
                ],
                "next_page_token": None,
            }
        return None

    def fake_artifact_size(url: str, headers: dict[str, str], timeout: int) -> int:
        if url.endswith(".zip"):
            return 23_603_522
        return 4096

    vcs = SubprocessGitGh(http_json_get=fake_http_json, http_artifact_size=fake_artifact_size)
    jobs = vcs.get_commit_ci_checks("owner/nightshift-desktop", "sha_circle_1", branch="feat/p8d")

    assert len(jobs) == 2
    # Workflow endpoint is queried once and cached for all jobs in that workflow.
    assert http_calls.count(f"https://circleci.com/api/v2/workflow/{wf_id}/job") == 1

    fe_job = next(j for j in jobs if j.name == "desktop-frontend")
    pkg_job = next(j for j in jobs if j.name == "desktop-package")

    # Build IDs match CircleCI CIRCLE_BUILD_NUM (not GitHub status IDs), and durations come from CircleCI.
    assert fe_job.build_id == "142"
    assert fe_job.duration_seconds > 470.0
    assert pkg_job.build_id == "145"
    assert pkg_job.duration_seconds > 160.0

    # Artifacts are populated with non-empty byte sizes bound to the CircleCI build number.
    ci_ev = validate_ci_evidence(
        expected_commit_sha="sha_circle_1",
        required_jobs=["desktop-frontend", "desktop-package"],
        observed_jobs=jobs,
        required_artifacts_by_job={
            "desktop-frontend": ["ui-screenshots/screenshot-manifest.json"],
            "desktop-package": ["nightshift-windows/NightShift-windows.zip"],
        },
    )
    assert ci_ev.verified is True

    # Screenshot manifest written with CIRCLE_BUILD_NUM="142" matches fe_job.build_id!
    manifest_path = _make_valid_manifest(
        tmp_path / "ui-screenshots",
        commit_sha="sha_circle_1",
        build_id="142",
    )
    shot_ev = validate_screenshot_manifest(
        manifest_path,
        expected_commit_sha="sha_circle_1",
        expected_build_id=fe_job.build_id,
    )
    assert shot_ev.verified is True


def test_subprocess_git_gh_github_actions_check_run_and_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nightshift.engine.vcs import SubprocessGitGh

    def fake_run(cmd: list[str], cwd: Any = None, timeout_seconds: int = 60) -> tuple[int, str, str, bool]:
        if cmd[:2] == ["gh", "api"] and "/check-runs" in cmd[2]:
            return (
                0,
                json.dumps(
                    {
                        "check_runs": [
                            {
                                "id": 8801,
                                "name": "android-build",
                                "status": "completed",
                                "conclusion": "success",
                                "head_sha": "sha_gha_1",
                                "started_at": "2026-11-27T10:00:00Z",
                                "completed_at": "2026-11-27T10:02:30Z",
                                "details_url": "https://github.com/owner/drivelift/actions/runs/9911/job/8801",
                            }
                        ]
                    }
                ),
                "",
                False,
            )
        if cmd[:2] == ["gh", "api"] and cmd[2].startswith("repos/owner/drivelift/actions/runs/9911/artifacts"):
            return (
                0,
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "name": "app-debug.apk",
                                "size_in_bytes": 18_000_000,
                                "expired": False,
                                "archive_download_url": "https://api.github.com/repos/owner/drivelift/actions/artifacts/1/zip",
                            },
                            {
                                "name": "expired.apk",
                                "size_in_bytes": 1000,
                                "expired": True,
                                "archive_download_url": "https://api.github.com/repos/owner/drivelift/actions/artifacts/2/zip",
                            },
                        ]
                    }
                ),
                "",
                False,
            )
        if cmd[:2] == ["gh", "api"] and "/status" in cmd[2]:
            return (
                0,
                json.dumps(
                    {
                        "sha": "sha_gha_1",
                        "statuses": [
                            {
                                "id": 555,
                                "context": "unknown-ci",
                                "state": "success",
                                "created_at": "2026-11-27T10:00:00Z",
                                "updated_at": "2026-11-27T10:05:00Z",
                                "target_url": "https://example.invalid/build/555",
                            }
                        ],
                    }
                ),
                "",
                False,
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr("nightshift.engine.vcs.run_subprocess_with_timeout", fake_run)

    vcs = SubprocessGitGh(http_json_get=lambda *_: None, http_artifact_size=lambda *_: 0)
    jobs = vcs.get_commit_ci_checks("owner/drivelift", "sha_gha_1", branch="feat/android")

    gha_job = next(j for j in jobs if j.name == "android-build")
    assert gha_job.duration_seconds == 150.0
    assert gha_job.build_id == "8801"
    assert len(gha_job.artifacts) == 1
    assert isinstance(gha_job.artifacts[0], CiArtifact)
    assert gha_job.artifacts[0].path == "app-debug.apk"
    assert gha_job.artifacts[0].size_bytes == 18_000_000

    res_gha = validate_ci_evidence(
        expected_commit_sha="sha_gha_1",
        required_jobs=["android-build"],
        observed_jobs=[gha_job],
        required_artifacts_by_job={"android-build": ["app-debug.apk"]},
    )
    assert res_gha.verified is True

    # Unknown external status never fabricates duration from GitHub status created_at/updated_at.
    unknown_job = next(j for j in jobs if j.name == "unknown-ci")
    assert unknown_job.duration_seconds == 0.0
    res_unknown = validate_ci_evidence(
        expected_commit_sha="sha_gha_1",
        required_jobs=["unknown-ci"],
        observed_jobs=[unknown_job],
    )
    assert res_unknown.verified is False
    assert res_unknown.rejection_reason == "suspicious_zero_duration_check"


def test_desktop_bridge_persists_project_evidence_config_for_android_and_desktop(tmp_path: Path) -> None:
    import threading
    from nightshift.bridge import BridgeAPI
    from nightshift.modules.registry import ModuleRegistry

    with Store(tmp_path / "bridge_evidence.db") as store:
        store.migrate()
        events = EventBus(store=store)
        started_configs: list[ProjectConfig] = []
        started_event = threading.Event()

        class _CaptureEngine:
            is_running = False

            async def execute_plan(
                self,
                project: ProjectConfig,
                phases: list[PhaseDefinition],
                *,
                plan_id: str = "plan_default",
            ) -> list[dict[str, Any]]:
                started_configs.append(project)
                started_event.set()
                return []

        bridge = BridgeAPI(store, events, ModuleRegistry(), engine=_CaptureEngine())

        # Save an Android project with its own CI job names, artifact rules, and screenshot manifest.
        save_resp = bridge.call(
            "settings.saveProject",
            {
                "id": "drivelift-android",
                "name": "DriveLift Android",
                "repo": "owner/drivelift-android",
                "local_path": str(tmp_path / "drivelift"),
                "artifact_glob": "*.apk",
                "required_ci_jobs": ["android-lint", "android-build"],
                "required_artifacts_by_job": {"android-build": ["app-debug.apk"]},
                "min_gate_duration_seconds": 3.0,
                "min_build_duration_seconds": 25.0,
                "screenshot_manifest_path": str(tmp_path / "android-shots" / "screenshot-manifest.json"),
            },
        )
        assert save_resp["ok"] is True

        # Updating basic project fields without evidence keys preserves the persisted evidence config.
        upd_resp = bridge.call(
            "settings.saveProject",
            {
                "id": "drivelift-android",
                "name": "DriveLift Android v2",
                "repo": "owner/drivelift-android",
                "local_path": str(tmp_path / "drivelift"),
                "artifact_glob": "*.apk",
            },
        )
        assert upd_resp["ok"] is True

        loaded_row = store.get_project("drivelift-android")
        assert loaded_row is not None
        loaded_cfg = ProjectConfig.from_dict(loaded_row)
        assert loaded_cfg.required_ci_jobs == ["android-lint", "android-build"]
        assert loaded_cfg.required_artifacts_by_job == {"android-build": ["app-debug.apk"]}
        assert loaded_cfg.min_gate_duration_seconds == 3.0
        assert loaded_cfg.min_build_duration_seconds == 25.0
        assert loaded_cfg.screenshot_manifest_path == str(tmp_path / "android-shots" / "screenshot-manifest.json")

        # Desktop-started run via engine.start receives the exact persisted evidence config.
        store.create_plan("plan-android", "drivelift-android", "Android Plan")
        store.upsert_phase("ph-1", "plan-android", "Fix UI", "feat/android-ui")
        start_resp = bridge.call("engine.start", {"projectId": "drivelift-android", "planId": "plan-android"})
        assert start_resp["ok"] is True
        assert started_event.wait(timeout=5.0)
        assert len(started_configs) == 1
        assert started_configs[0].required_ci_jobs == ["android-lint", "android-build"]
        assert started_configs[0].required_artifacts_by_job == {"android-build": ["app-debug.apk"]}
        assert started_configs[0].screenshot_manifest_path == str(tmp_path / "android-shots" / "screenshot-manifest.json")

        # Queue import also persists custom evidence requirements.
        imp_resp = bridge.call(
            "plan.importQueue",
            {
                "queue": {
                    "repo": "owner/custom-queue",
                    "local_repo": str(tmp_path / "custom"),
                    "required_ci_jobs": ["unit-tests", "package-win"],
                    "required_artifacts_by_job": {"package-win": ["dist/*.zip"]},
                    "min_gate_duration_seconds": 4.0,
                    "min_build_duration_seconds": 20.0,
                    "screenshot_manifest_path": "shots/screenshot-manifest.json",
                    "phases": [{"id": "q1", "title": "Q1", "branch": "feat/q1"}],
                }
            },
        )
        assert imp_resp["ok"] is True
        q_row = store.get_project("owner/custom-queue")
        assert q_row is not None
        q_cfg = ProjectConfig.from_dict(q_row)
        assert q_cfg.required_ci_jobs == ["unit-tests", "package-win"]
        assert q_cfg.required_artifacts_by_job == {"package-win": ["dist/*.zip"]}
        assert q_cfg.min_gate_duration_seconds == 4.0
        assert q_cfg.min_build_duration_seconds == 20.0
        assert q_cfg.screenshot_manifest_path == "shots/screenshot-manifest.json"
