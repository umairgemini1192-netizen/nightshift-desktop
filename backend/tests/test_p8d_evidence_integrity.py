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
    _make_valid_manifest(
        tmp_path / "ui-screenshots",
        commit_sha="sha_head_804",
        build_id="pre-3",
    )
    vcs.register_job_artifacts_from_dir(
        "pre-3",
        tmp_path / "ui-screenshots",
        artifact_prefix="ui-screenshots",
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
            screenshot_manifest_path="ui-screenshots/screenshot-manifest.json",
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
    pipe_id = "6b33c8ab-90b5-4a12-90f8-123456789abc"
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
        if url == f"https://circleci.com/api/v2/workflow/{wf_id}":
            return {
                "id": wf_id,
                "pipeline_id": pipe_id,
                "project_slug": slug,
                "status": "success",
            }
        if url == f"https://circleci.com/api/v2/pipeline/{pipe_id}":
            return {
                "id": pipe_id,
                "project_slug": slug,
                "vcs": {"revision": "sha_circle_1", "branch": "feat/p8d"},
            }
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
    # Workflow jobs, workflow metadata, and pipeline revision are queried once and cached for all jobs.
    assert http_calls.count(f"https://circleci.com/api/v2/workflow/{wf_id}/job") == 1
    assert http_calls.count(f"https://circleci.com/api/v2/workflow/{wf_id}") == 1
    assert http_calls.count(f"https://circleci.com/api/v2/pipeline/{pipe_id}") == 1

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


def test_subprocess_git_gh_rejects_circleci_build_with_mismatched_or_missing_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nightshift.engine.vcs import SubprocessGitGh

    wf_stale = "11111111-1111-4111-8111-111111111111"
    pipe_stale = "22222222-2222-4222-8222-222222222222"
    wf_missing = "33333333-3333-4333-8333-333333333333"
    pipe_legacy_bad = "44444444-4444-4444-8444-444444444444"
    slug = "circleci/org123/proj456"

    def fake_run(cmd: list[str], cwd: Any = None, timeout_seconds: int = 60) -> tuple[int, str, str, bool]:
        if cmd[:2] == ["gh", "api"] and "/check-runs" in cmd[2]:
            return (0, json.dumps({"total_count": 0, "check_runs": []}), "", False)
        if cmd[:2] == ["gh", "api"] and "/status" in cmd[2]:
            return (
                0,
                json.dumps(
                    {
                        "sha": "sha_new_2",
                        "state": "success",
                        "statuses": [
                            {
                                "id": 7001,
                                "context": "ci/circleci: desktop-backend",
                                "state": "success",
                                "target_url": f"https://app.circleci.com/workflow/{wf_stale}/job/aaaa",
                            },
                            {
                                "id": 7002,
                                "context": "ci/circleci: desktop-gates",
                                "state": "success",
                                "target_url": f"https://app.circleci.com/workflow/{wf_missing}/job/bbbb",
                            },
                            {
                                "id": 7003,
                                "context": "ci/circleci: desktop-package",
                                "state": "success",
                                "target_url": "https://circleci.com/gh/owner/nightshift-desktop/77",
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
        if url == f"https://circleci.com/api/v2/workflow/{wf_stale}":
            return {"id": wf_stale, "pipeline_id": pipe_stale}
        if url == f"https://circleci.com/api/v2/pipeline/{pipe_stale}":
            return {"id": pipe_stale, "vcs": {"revision": "sha_old_1"}}
        if url == f"https://circleci.com/api/v2/workflow/{wf_stale}/job":
            return {
                "items": [
                    {
                        "id": "aaaa",
                        "job_number": 90,
                        "name": "desktop-backend",
                        "project_slug": slug,
                        "status": "success",
                        "started_at": "2026-11-27T21:00:00Z",
                        "stopped_at": "2026-11-27T21:05:00Z",
                    }
                ]
            }
        if url == f"https://circleci.com/api/v2/workflow/{wf_missing}":
            return None
        if url == f"https://circleci.com/api/v2/workflow/{wf_missing}/job":
            return {
                "items": [
                    {
                        "id": "bbbb",
                        "job_number": 91,
                        "name": "desktop-gates",
                        "project_slug": slug,
                        "status": "success",
                        "started_at": "2026-11-27T21:00:00Z",
                        "stopped_at": "2026-11-27T21:01:00Z",
                    }
                ]
            }
        if url == "https://circleci.com/api/v2/project/gh/owner/nightshift-desktop/job/77":
            return {
                "status": "success",
                "started_at": "2026-11-27T21:00:00Z",
                "stopped_at": "2026-11-27T21:03:00Z",
                "pipeline": {"id": pipe_legacy_bad},
            }
        if url == f"https://circleci.com/api/v2/pipeline/{pipe_legacy_bad}":
            return {"id": pipe_legacy_bad, "vcs": {"revision": "sha_legacy_old"}}
        return None

    vcs = SubprocessGitGh(http_json_get=fake_http_json, http_artifact_size=lambda *_: 4096)
    jobs = vcs.get_commit_ci_checks("owner/nightshift-desktop", "sha_new_2", branch="feat/p8d")

    by_name = {j.name: j for j in jobs}
    # Older CircleCI build is NOT relabeled to sha_new_2; duration & artifacts are rejected.
    assert by_name["desktop-backend"].commit_sha == "sha_old_1"
    assert by_name["desktop-backend"].duration_seconds == 0.0
    assert by_name["desktop-backend"].artifacts == []

    # Unresolvable CircleCI revision fails closed with empty commit_sha.
    assert by_name["desktop-gates"].commit_sha == ""
    assert by_name["desktop-gates"].duration_seconds == 0.0
    assert by_name["desktop-gates"].artifacts == []

    # Legacy direct CircleCI job URL also checks pipeline revision and rejects mismatch.
    assert by_name["desktop-package"].commit_sha == "sha_legacy_old"
    assert by_name["desktop-package"].duration_seconds == 0.0
    assert by_name["desktop-package"].artifacts == []

    # Artifacts endpoint must never be fetched for mismatched/unverified revisions.
    assert not any("/artifacts" in u for u in http_calls)

    ev = validate_ci_evidence(
        expected_commit_sha="sha_new_2",
        required_jobs=["desktop-backend", "desktop-gates", "desktop-package"],
        observed_jobs=jobs,
    )
    assert ev.verified is False
    assert ev.rejection_reason == "stale_commit_sha"


@pytest.mark.asyncio
async def test_e2e_screenshot_manifest_requires_verified_producer_for_custom_job_names(tmp_path: Path) -> None:
    # 1. Custom Android job names with NO screenshot_job_name configured: must fail closed!
    vcs1 = FakeGitGh()
    vcs1.add_pr("feat/android-noproducer", number=820, head_sha="sha_and_1")
    vcs1.commit_ci_checks["sha_and_1"] = [
        CiJobCheck(
            name="android-lint",
            status="success",
            commit_sha="sha_and_1",
            build_id="and-10",
            duration_seconds=20.0,
            branch="feat/android-noproducer",
        ),
        CiJobCheck(
            name="android-ui-tests",
            status="success",
            commit_sha="sha_and_1",
            build_id="and-42",
            duration_seconds=95.0,
            artifacts=[CiArtifact(path="shots/screenshot-manifest.json", size_bytes=1024, build_id="and-42")],
            branch="feat/android-noproducer",
        ),
    ]
    unrelated_manifest = _make_valid_manifest(
        tmp_path / "shots-unrelated",
        commit_sha="sha_and_1",
        build_id="unrelated-999",
    )
    store1, engine1 = _build_engine(tmp_path / "s1", vcs1)
    try:
        project1 = ProjectConfig(
            id="proj-and-1",
            name="DriveLift",
            repo="owner/drivelift",
            local_path=tmp_path / "repo1",
            output_dir=tmp_path / "out1",
            required_ci_jobs=["android-lint", "android-ui-tests"],
            screenshot_manifest_path=str(unrelated_manifest),
        )
        phase1 = PhaseDefinition(
            id="p-and-1",
            title="Android UI without producer job configured",
            prompt="Implement UI",
            branch="feat/android-noproducer",
            auto_merge=True,
            release_after_merge=False,
        )
        rows1 = await engine1.execute_plan(project1, [phase1], plan_id="plan-and-1")
        assert rows1[0]["status"] == "PARKED"
        assert "missing_screenshot_producer_job" in rows1[0]["detail"]
        assert "feat/android-noproducer" not in vcs1.merged_prs
    finally:
        store1.close()

    # 2. Custom Android screenshot_job_name configured, but manifest build_id does NOT match: rejected!
    vcs2 = FakeGitGh()
    vcs2.add_pr("feat/android-badbuild", number=821, head_sha="sha_and_2")
    vcs2.commit_ci_checks["sha_and_2"] = [
        CiJobCheck(
            name="android-ui-tests",
            status="success",
            commit_sha="sha_and_2",
            build_id="and-42",
            duration_seconds=95.0,
            artifacts=[CiArtifact(path="shots/screenshot-manifest.json", size_bytes=512, build_id="and-42")],
            branch="feat/android-badbuild",
        ),
    ]
    _make_valid_manifest(
        tmp_path / "shots-badbuild",
        commit_sha="sha_and_2",
        build_id="unrelated-999",
    )
    vcs2.register_job_artifacts_from_dir("and-42", tmp_path / "shots-badbuild", artifact_prefix="shots")
    store2, engine2 = _build_engine(tmp_path / "s2", vcs2)
    try:
        project2 = ProjectConfig(
            id="proj-and-2",
            name="DriveLift",
            repo="owner/drivelift",
            local_path=tmp_path / "repo2",
            output_dir=tmp_path / "out2",
            required_ci_jobs=["android-ui-tests"],
            screenshot_manifest_path="shots/screenshot-manifest.json",
            screenshot_job_name="android-ui-tests",
        )
        phase2 = PhaseDefinition(
            id="p-and-2",
            title="Android UI mismatched build id",
            prompt="Implement UI",
            branch="feat/android-badbuild",
            auto_merge=True,
            release_after_merge=False,
        )
        rows2 = await engine2.execute_plan(project2, [phase2], plan_id="plan-and-2")
        assert rows2[0]["status"] == "PARKED"
        assert "screenshot_build_id_mismatch" in rows2[0]["detail"]
        assert "feat/android-badbuild" not in vcs2.merged_prs
    finally:
        store2.close()

    # 3. Custom Android screenshot_job_name configured AND manifest uploaded by verified producer: passes!
    vcs3 = FakeGitGh()
    vcs3.add_pr("feat/android-good", number=822, head_sha="sha_and_3")
    vcs3.commit_ci_checks["sha_and_3"] = [
        CiJobCheck(
            name="android-ui-tests",
            status="success",
            commit_sha="sha_and_3",
            build_id="and-42",
            duration_seconds=95.0,
            artifacts=[CiArtifact(path="shots/screenshot-manifest.json", size_bytes=512, build_id="and-42")],
            branch="feat/android-good",
        ),
    ]
    vcs3.commit_ci_checks["sha_merge_123"] = [
        CiJobCheck(
            name="android-ui-tests",
            status="success",
            commit_sha="sha_merge_123",
            build_id="and-post-43",
            duration_seconds=95.0,
            branch="main",
        ),
    ]
    _make_valid_manifest(
        tmp_path / "shots-good",
        commit_sha="sha_and_3",
        build_id="and-42",
    )
    vcs3.register_job_artifacts_from_dir("and-42", tmp_path / "shots-good", artifact_prefix="shots")
    store3, engine3 = _build_engine(tmp_path / "s3", vcs3)
    try:
        project3 = ProjectConfig(
            id="proj-and-3",
            name="DriveLift",
            repo="owner/drivelift",
            local_path=tmp_path / "repo3",
            output_dir=tmp_path / "out3",
            required_ci_jobs=["android-ui-tests"],
            screenshot_manifest_path="shots/screenshot-manifest.json",
            screenshot_job_name="android-ui-tests",
        )
        phase3 = PhaseDefinition(
            id="p-and-3",
            title="Android UI matching producer job",
            prompt="Implement UI",
            branch="feat/android-good",
            auto_merge=True,
            release_after_merge=False,
        )
        rows3 = await engine3.execute_plan(project3, [phase3], plan_id="plan-and-3")
        assert rows3[0]["status"] == "COMPLETED", rows3[0].get("detail")
        assert "feat/android-good" in vcs3.merged_prs
    finally:
        store3.close()


def test_queue_export_import_preserves_duration_thresholds_and_screenshot_job_name(tmp_path: Path) -> None:
    from nightshift.bridge import BridgeAPI
    from nightshift.engine.planner import export_queue_json, import_queue_json
    from nightshift.modules.registry import ModuleRegistry

    with Store(tmp_path / "src.db") as src_store:
        src_store.migrate()
        proj = src_store.create_project(
            "owner/custom-thresholds",
            "Custom Thresholds",
            "owner/custom-thresholds",
            str(tmp_path / "repo"),
            required_ci_jobs_json=json.dumps(["fast-gate", "android-ui-tests"]),
            required_artifacts_by_job_json=json.dumps({"android-ui-tests": ["shots/screenshot-manifest.json"]}),
            min_gate_duration_seconds=1.5,
            min_build_duration_seconds=2.5,
            screenshot_manifest_path="shots/screenshot-manifest.json",
            screenshot_job_name="android-ui-tests",
        )
        plan = src_store.create_plan("plan-th", proj["id"], "Threshold Plan")
        phase = PhaseDefinition(id="p1", title="P1", branch="feat/p1")

        exported = export_queue_json(proj, plan, [phase])
        assert exported["min_gate_duration_seconds"] == 1.5
        assert exported["min_build_duration_seconds"] == 2.5
        assert exported["screenshot_manifest_path"] == "shots/screenshot-manifest.json"
        assert exported["screenshot_job_name"] == "android-ui-tests"

        reimported = import_queue_json(exported)
        assert reimported["project"]["min_gate_duration_seconds"] == 1.5
        assert reimported["project"]["min_build_duration_seconds"] == 2.5
        assert reimported["project"]["screenshot_job_name"] == "android-ui-tests"

    # Full round-trip into a fresh Store via BridgeAPI plan.importQueue
    with Store(tmp_path / "dst.db") as dst_store:
        dst_store.migrate()
        bridge = BridgeAPI(dst_store, EventBus(store=dst_store), ModuleRegistry())
        resp = bridge.call("plan.importQueue", {"queue": exported})
        assert resp["ok"] is True

        fresh_row = dst_store.get_project("owner/custom-thresholds")
        assert fresh_row is not None
        cfg = ProjectConfig.from_dict(fresh_row)
        assert cfg.min_gate_duration_seconds == 1.5
        assert cfg.min_build_duration_seconds == 2.5
        assert cfg.screenshot_manifest_path == "shots/screenshot-manifest.json"
        assert cfg.screenshot_job_name == "android-ui-tests"


# ---------------------------------------------------------------------------
# 6. Regression tests for producer-build screenshot retrieval & GHA provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_rejects_forged_local_screenshot_manifest_not_uploaded_by_producer_build(
    tmp_path: Path,
) -> None:
    """P1 regression: a local manifest copying expected SHA + build_id must never satisfy the gate."""
    vcs = FakeGitGh()
    vcs.add_pr("feat/forged-local-shots", number=830, head_sha="sha_forged_1")
    vcs.commit_ci_checks["sha_forged_1"] = _make_valid_jobs(
        "sha_forged_1",
        branch="feat/forged-local-shots",
        build_prefix="pre",
    )
    # Author a forged local manifest on disk with the EXACT expected commit_sha and producer build_id,
    # and even upload artifacts to a DIFFERENT build ("pre-stale"), but NOT to verified build "pre-3".
    forged_dir = tmp_path / "forged-local-shots"
    forged_manifest = _make_valid_manifest(
        forged_dir,
        commit_sha="sha_forged_1",
        build_id="pre-3",
    )
    vcs.register_job_artifacts_from_dir("pre-stale", forged_dir, artifact_prefix="ui-screenshots")

    store, engine = _build_engine(tmp_path / "s_forged", vcs)
    try:
        project = ProjectConfig(
            id="proj-forged-shots",
            name="NightShift",
            repo="owner/nightshift",
            local_path=tmp_path / "repo_forged",
            output_dir=tmp_path / "out_forged",
            required_ci_jobs=["desktop-gates", "desktop-backend", "desktop-frontend", "desktop-package"],
            screenshot_manifest_path=str(forged_manifest),
        )
        phase = PhaseDefinition(
            id="p-forged-shots",
            title="Forged local screenshot manifest test",
            prompt="Implement UI",
            branch="feat/forged-local-shots",
            auto_merge=True,
            release_after_merge=False,
        )
        rows = await engine.execute_plan(project, [phase], plan_id="plan-forged")
        assert rows[0]["status"] == "PARKED"
        assert "missing_screenshot_manifest" in rows[0]["detail"]
        assert "feat/forged-local-shots" not in vcs.merged_prs
    finally:
        store.close()


def test_subprocess_git_gh_downloads_screenshot_evidence_from_verified_circleci_build(
    tmp_path: Path,
) -> None:
    """P1 regression: SubprocessGitGh downloads manifest + referenced PNGs from the exact verified CircleCI build."""
    from nightshift.engine.vcs import SubprocessGitGh

    # Build authentic manifest + PNG bytes in a source dir to serve over fake HTTP
    src_dir = tmp_path / "ci-output"
    manifest_file = _make_valid_manifest(
        src_dir,
        commit_sha="sha_circle_shot_1",
        build_id="142",
        renderer="playwright-mockbridge",
    )
    manifest_bytes = manifest_file.read_bytes()
    manifest_data = json.loads(manifest_bytes.decode("utf-8"))

    url_map: dict[str, bytes] = {
        "https://output.circle-artifacts.com/job/142/ui-screenshots/screenshot-manifest.json": manifest_bytes,
    }
    artifacts: list[CiArtifact] = [
        CiArtifact(
            path="ui-screenshots/screenshot-manifest.json",
            size_bytes=len(manifest_bytes),
            build_id="142",
            url="https://output.circle-artifacts.com/job/142/ui-screenshots/screenshot-manifest.json",
        )
    ]
    for entry in manifest_data["screenshots"]:
        fname = entry["file"]
        png_bytes = (src_dir / fname).read_bytes()
        url = f"https://output.circle-artifacts.com/job/142/ui-screenshots/{fname}"
        url_map[url] = png_bytes
        artifacts.append(
            CiArtifact(
                path=f"ui-screenshots/{fname}",
                size_bytes=len(png_bytes),
                build_id="142",
                url=url,
            )
        )

    # Also add a decoy artifact from a different build_id that must be ignored
    artifacts.append(
        CiArtifact(
            path="ui-screenshots/decoy.png",
            size_bytes=1234,
            build_id="999",
            url="https://output.circle-artifacts.com/job/999/ui-screenshots/decoy.png",
        )
    )

    fetched_urls: list[str] = []

    def fake_bytes_get(url: str, headers: dict[str, str], timeout: int) -> bytes | None:
        fetched_urls.append(url)
        return url_map.get(url)

    producer_job = CiJobCheck(
        name="desktop-frontend",
        status="success",
        commit_sha="sha_circle_shot_1",
        build_id="142",
        duration_seconds=120.0,
        artifacts=artifacts,
        branch="feat/p8d",
    )

    vcs = SubprocessGitGh(http_bytes_get=fake_bytes_get)
    stage_dir = tmp_path / "stage_ok"
    downloaded, err = vcs.download_job_screenshot_evidence(
        "owner/nightshift-desktop",
        producer_job,
        "ui-screenshots/screenshot-manifest.json",
        stage_dir,
    )
    assert err == ""
    assert downloaded is not None
    assert downloaded.exists()
    assert len(fetched_urls) == 1 + len(manifest_data["screenshots"])
    assert not any("/job/999/" in u for u in fetched_urls)

    val = validate_screenshot_manifest(
        downloaded,
        expected_commit_sha="sha_circle_shot_1",
        expected_build_id="142",
        screenshots_root=downloaded.parent,
    )
    assert val.verified is True

    # If one referenced PNG is missing from the producer build's artifacts, validation must fail closed.
    incomplete_job = CiJobCheck(
        name="desktop-frontend",
        status="success",
        commit_sha="sha_circle_shot_1",
        build_id="142",
        duration_seconds=120.0,
        artifacts=artifacts[:2],  # manifest + only 1 of 3 PNGs
        branch="feat/p8d",
    )
    stage_inc = tmp_path / "stage_incomplete"
    dl_inc, err_inc = vcs.download_job_screenshot_evidence(
        "owner/nightshift-desktop",
        incomplete_job,
        "ui-screenshots/screenshot-manifest.json",
        stage_inc,
    )
    assert err_inc == ""
    assert dl_inc is not None
    val_inc = validate_screenshot_manifest(
        dl_inc,
        expected_commit_sha="sha_circle_shot_1",
        expected_build_id="142",
        screenshots_root=dl_inc.parent,
    )
    assert val_inc.verified is False
    assert val_inc.rejection_reason == "missing_screenshot_file"


def test_subprocess_git_gh_preserves_gha_workflow_run_artifact_provenance_for_multi_job_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 regression: multi-job GHA workflow artifacts keep workflow-run scope instead of caller job IDs."""
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
                                "name": "android-lint",
                                "status": "completed",
                                "conclusion": "success",
                                "head_sha": "sha_multi_1",
                                "started_at": "2026-11-27T10:00:00Z",
                                "completed_at": "2026-11-27T10:01:00Z",
                                "details_url": "https://github.com/owner/drivelift/actions/runs/9911/job/8801",
                            },
                            {
                                "id": 8802,
                                "name": "android-build",
                                "status": "completed",
                                "conclusion": "success",
                                "head_sha": "sha_multi_1",
                                "started_at": "2026-11-27T10:01:00Z",
                                "completed_at": "2026-11-27T10:04:00Z",
                                "details_url": "https://github.com/owner/drivelift/actions/runs/9911/job/8802",
                            },
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
                                "workflow_run": {"id": 9911, "head_sha": "sha_multi_1"},
                            }
                        ]
                    }
                ),
                "",
                False,
            )
        if cmd[:2] == ["gh", "api"] and "/status" in cmd[2]:
            return (0, json.dumps({"sha": "sha_multi_1", "statuses": []}), "", False)
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr("nightshift.engine.vcs.run_subprocess_with_timeout", fake_run)

    vcs = SubprocessGitGh(http_json_get=lambda *_: None, http_artifact_size=lambda *_: 0)
    jobs = vcs.get_commit_ci_checks("owner/drivelift", "sha_multi_1", branch="feat/android")
    assert len(jobs) == 2

    lint_job = next(j for j in jobs if j.name == "android-lint")
    build_job = next(j for j in jobs if j.name == "android-build")

    # Neither job stamps its own job ID onto the workflow-run artifact!
    assert lint_job.build_id == "8801"
    assert build_job.build_id == "8802"
    assert lint_job.workflow_run_id == "9911"
    assert build_job.workflow_run_id == "9911"
    assert lint_job.workflow_run_job_count == 2
    assert build_job.workflow_run_job_count == 2

    assert isinstance(lint_job.artifacts[0], CiArtifact)
    assert isinstance(build_job.artifacts[0], CiArtifact)
    assert lint_job.artifacts[0].build_id == "workflow-run:9911"
    assert lint_job.artifacts[0].scope == "workflow_run"
    assert build_job.artifacts[0].build_id == "workflow-run:9911"
    assert build_job.artifacts[0].scope == "workflow_run"

    # Job-specific artifact requirement on either job is rejected because workflow-run scope is shared by 2 jobs.
    res_lint = validate_ci_evidence(
        expected_commit_sha="sha_multi_1",
        required_jobs=["android-lint"],
        observed_jobs=jobs,
        required_artifacts_by_job={"android-lint": ["app-debug.apk"]},
    )
    assert res_lint.verified is False
    assert res_lint.rejection_reason == "missing_required_artifact"
    assert "producer provenance cannot be verified" in res_lint.details[0]

    # Even if only one job is passed to validate_ci_evidence, workflow_run_job_count == 2 still rejects it!
    res_build_single = validate_ci_evidence(
        expected_commit_sha="sha_multi_1",
        required_jobs=["android-build"],
        observed_jobs=[build_job],
        required_artifacts_by_job={"android-build": ["app-debug.apk"]},
    )
    assert res_build_single.verified is False
    assert res_build_single.rejection_reason == "missing_required_artifact"


def test_subprocess_git_gh_rejects_stale_gha_status_job_and_mismatched_workflow_run_head_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end audit hardening: stale GHA commit status job SHA and mismatched watch_workflow_run headSha fail closed."""
    from nightshift.engine.vcs import SubprocessGitGh

    def fake_run(cmd: list[str], cwd: Any = None, timeout_seconds: int = 60) -> tuple[int, str, str, bool]:
        if cmd[:2] == ["gh", "api"] and "/check-runs" in cmd[2]:
            return (0, json.dumps({"check_runs": []}), "", False)
        if cmd[:2] == ["gh", "api"] and "/status" in cmd[2]:
            return (
                0,
                json.dumps(
                    {
                        "sha": "sha_new_head",
                        "statuses": [
                            {
                                "id": 700,
                                "context": "android-build",
                                "state": "success",
                                "target_url": "https://github.com/owner/drivelift/actions/runs/500/job/501",
                            }
                        ],
                    }
                ),
                "",
                False,
            )
        if cmd[:2] == ["gh", "api"] and cmd[2] == "repos/owner/drivelift/actions/jobs/501":
            return (
                0,
                json.dumps(
                    {
                        "id": 501,
                        "run_id": 500,
                        "head_sha": "sha_stale_old",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-11-27T10:00:00Z",
                        "completed_at": "2026-11-27T10:05:00Z",
                    }
                ),
                "",
                False,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            return (
                0,
                json.dumps(
                    [
                        {
                            "databaseId": 7777,
                            "status": "completed",
                            "conclusion": "success",
                            "headSha": "sha_different_merge",
                        }
                    ]
                ),
                "",
                False,
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr("nightshift.engine.vcs.run_subprocess_with_timeout", fake_run)

    vcs = SubprocessGitGh(http_json_get=lambda *_: None, http_artifact_size=lambda *_: 0)
    jobs = vcs.get_commit_ci_checks("owner/drivelift", "sha_new_head", branch="feat/android")
    assert len(jobs) == 1
    assert jobs[0].commit_sha == "sha_stale_old"
    assert jobs[0].duration_seconds == 0.0
    assert jobs[0].artifacts == []

    ok, msg, run_id = vcs.watch_workflow_run(
        "owner/drivelift",
        branch="main",
        workflow="Android build",
        commit_sha="sha_expected_merge",
    )
    assert ok is None
    assert run_id is None
    assert "headSha" in msg


def test_persist_ci_evidence_and_merge_gate_enforce_exact_sha_and_build_binding(tmp_path: Path) -> None:
    """End-to-end audit hardening: stale job rows never persist verified=1, and MergeGate binds screenshot build_id."""
    with Store(tmp_path / "bind.db") as store:
        store.migrate()
        task, contract = _setup_task(store, task_id="t-bind", branch="feat/bind")
        store.record_task_owner_approval("t-bind", "owner", "sha_fresh")
        task = store.get_task("t-bind")
        assert task is not None

        stale_job = CiJobCheck(
            name="desktop-backend",
            status="success",
            commit_sha="sha_old",
            build_id="old-1",
            duration_seconds=30.0,
        )
        fresh_job = CiJobCheck(
            name="desktop-backend",
            status="success",
            commit_sha="sha_fresh",
            build_id="fresh-2",
            duration_seconds=35.0,
        )
        val = validate_ci_evidence(
            expected_commit_sha="sha_fresh",
            required_jobs=["desktop-backend"],
            observed_jobs=[stale_job, fresh_job],
        )
        assert val.verified is True
        persist_ci_evidence(
            store,
            task_id="t-bind",
            contract_sha256=contract["contract_sha256"],
            commit_sha="sha_fresh",
            kind="ci_check",
            observed_jobs=[stale_job, fresh_job],
            validation=val,
        )
        all_rows = store.list_evidence_for_task("t-bind")
        stale_row = next(r for r in all_rows if r["commit_sha"] == "sha_old")
        fresh_row = next(r for r in all_rows if r["commit_sha"] == "sha_fresh")
        assert stale_row["verified"] == 0
        assert fresh_row["verified"] == 1

        # MergeGate rejects screenshot evidence whose build_id does not match any verified CI build_id on head_sha.
        gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=False)
        bad_shot = {
            "kind": "screenshot",
            "source": "screenshot-manifest:playwright-mockbridge",
            "commit_sha": "sha_fresh",
            "contract_sha256": contract["contract_sha256"],
            "build_id": "unrelated-build-999",
            "duration_seconds": 0.0,
            "verified": 1,
            "metadata_json": "{}",
        }
        res_bad = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_fresh",
            evidence_records=[fresh_row, bad_shot],
            required_ci_jobs=["desktop-backend"],
            require_screenshot_evidence=True,
        )
        assert res_bad.allowed is False
        assert res_bad.block_reason == "missing_verified_screenshots"
