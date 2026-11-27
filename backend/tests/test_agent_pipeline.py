"""Comprehensive tests for the Role-Based Agent Pipeline (Phases 8b-8f, Amendments A-J)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from nightshift.bridge import BridgeAPI
from nightshift.engine.pipeline_contracts import (
    DiffPathEntry,
    MergeGate,
    canonical_contract_json,
    check_contract_path_guards,
    compute_contract_sha256,
    detect_assertion_relaxation_for_files,
    parse_name_status_diff,
    verify_contract_integrity,
)
from nightshift.engine.pipeline_evidence import (
    CiJobCheck,
    persist_ci_evidence,
    render_mechanical_pr_body,
    validate_ci_evidence,
    validate_screenshot_manifest,
)
from nightshift.engine.pipeline_orchestrator import (
    InvalidTransitionError,
    LeaseConflictError,
    TaskOrchestrator,
    network_backoff_delay,
)
from nightshift.engine.pipeline_review import (
    ReleaseGovernance,
    ReviewerCandidate,
    build_independent_review_prompt,
    classify_task_risk,
    compute_risk_score,
    engine_provider,
    select_independent_reviewer,
    validate_review_response,
)
from nightshift.engine.pipeline_router import (
    BudgetGuard,
    BudgetLimits,
    PipelineEconomics,
    PipelineRouter,
    RouterEngineSpec,
    summarize_ci_failure_triage,
)
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.runners.base import build_scrubbed_env
from nightshift.store import Store


def _seed_project_plan_phase(store: Store) -> tuple[str, str, str]:
    store.create_project("proj-1", "NightShift", "owner/nightshift", "D:/repo")
    store.create_plan("plan-1", "proj-1", "Agent Pipeline", "Build pipeline")
    store.upsert_phase("phase-1", "plan-1", "Phase 8b", "feat/p8b")
    return "proj-1", "plan-1", "phase-1"


# =====================================================================
# 1. Phase 8b: Trust boundaries, contracts, protected tests, MergeGate
# =====================================================================


def test_contract_canonical_immutability_and_revision_invalidation(tmp_path: Path) -> None:
    with Store(tmp_path / "ns.db") as store:
        store.migrate()
        _, plan_id, phase_id = _seed_project_plan_phase(store)
        task, contract = store.create_task_with_contract(
            task_id="t-1",
            phase_id=phase_id,
            plan_id=plan_id,
            title="Enforce allowed globs",
            branch="feat/t-1",
            risk_class="business_logic",
            risk_factors={"impact": 3, "data_sensitivity": 1, "reversibility": 2, "test_coverage": 4, "uncertainty": 2},
            acceptance_criteria=[{"id": "ac-1", "description": "Blocks outside globs"}],
            allowed_globs=["backend/nightshift/engine/**"],
            forbidden_globs=[".circleci/**"],
            budget={"max_attempts": 4},
        )

        # Canonical JSON must NOT embed contract_sha256
        parsed = json.loads(contract["canonical_json"])
        assert "contract_sha256" not in parsed
        assert compute_contract_sha256(contract["canonical_json"]) == contract["contract_sha256"]
        assert verify_contract_integrity(contract, expected_sha256=task["contract_sha256"])[0] is True

        # SQLite BEFORE UPDATE and BEFORE DELETE triggers enforce immutability
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute("UPDATE task_contracts SET risk_class = 'docs_format' WHERE id = ?", (contract["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.connection.execute("DELETE FROM task_contracts WHERE id = ?", (contract["id"],))

        # Record prior evidence & owner approval, then bump contract version -> must invalidate both
        store.record_evidence(
            task_id="t-1",
            contract_sha256=contract["contract_sha256"],
            commit_sha="aaa111",
            kind="ci_check",
            source="circleci:desktop-backend",
            duration_seconds=30.0,
            verified=True,
        )
        store.update_task_fields("t-1", owner_approved_sha="aaa111", review_sha="aaa111", approved_sha="aaa111")

        revised_task, revised_contract = store.revise_task_contract(
            task_id="t-1",
            risk_class="business_logic",
            risk_factors={"impact": 3, "data_sensitivity": 1, "reversibility": 2, "test_coverage": 4, "uncertainty": 2},
            acceptance_criteria=[{"id": "ac-1", "description": "Blocks outside globs"}],
            allowed_globs=["backend/nightshift/engine/**", "backend/nightshift/store.py"],
            forbidden_globs=[".circleci/**"],
            budget={"max_attempts": 4},
        )
        assert revised_contract["version"] == 2
        assert revised_task["contract_sha256"] == revised_contract["contract_sha256"]
        assert revised_task["owner_approved_sha"] is None
        assert revised_task["review_sha"] is None
        ev = store.list_evidence_records("t-1")
        assert ev and ev[0]["verified"] == 0
        assert ev[0]["rejection_reason"] == "invalidated_by_contract_revision"


def test_name_status_rename_case_and_protected_paths() -> None:
    raw = "\n".join(
        [
            "R100\tbackend/tests/conftest.py\tbackend/nightshift/helper.py",
            "M\tFrontend/Tests/App.Spec.Js",
            "M\t.circleci/config.yml",
            "M\tdesign/tokens.json",
        ]
    )
    entries = parse_name_status_diff(raw)
    assert len(entries) == 4
    assert entries[0].old_path == "backend/tests/conftest.py"
    assert entries[0].path == "backend/nightshift/helper.py"

    findings, touches_gov = check_contract_path_guards(
        entries,
        allowed_globs=["backend/nightshift/**"],
        case_fold=True,
    )
    assert touches_gov is True
    guards = {f.guard for f in findings}
    assert "outside_allowed_globs" in guards
    assert "governance_split_required" in guards


def test_assertion_relaxation_blocks_python_and_js_and_waiver_rules() -> None:
    py_before = "def test_ok():\n    assert 1 == 1\n    assert 2 == 2\n"
    py_after = "def test_ok():\n    assert 1 == 1\n"
    py_findings = detect_assertion_relaxation_for_files({"backend/tests/test_demo.py": (py_before, py_after)})
    assert any(f.guard == "assertion_relaxation" for f in py_findings)

    js_before = "test('a', () => { expect(x).toBe(1); const r = new AxeBuilder({ page }); });"
    js_after = "test.skip('a', () => { expect(x).toBeDefined(); });"
    js_findings = detect_assertion_relaxation_for_files({"frontend/tests/demo.spec.ts": (js_before, js_after)})
    assert len(js_findings) >= 2

    # MergeGate blocks assertion relaxation unconditionally on governance PR without exact-SHA waiver
    payload = {
        "task_id": "t-gov",
        "version": 1,
        "allowed_globs": ["backend/tests/**"],
        "forbidden_globs": [],
        "risk_class": "governance",
    }
    canon = canonical_contract_json(payload)
    digest = compute_contract_sha256(canon)
    contract = {
        "canonical_json": canon,
        "contract_sha256": digest,
        "allowed_globs_json": json.dumps(["backend/tests/**"]),
        "forbidden_globs_json": "[]",
        "risk_class": "governance",
        "owner_approval_required": 1,
    }
    task = {
        "id": "t-gov",
        "risk_class": "governance",
        "contract_sha256": digest,
        "contributing_providers_json": json.dumps(["google"]),
        "owner_approved_sha": "sha123",
    }
    gate = MergeGate(autonomous_merge_enabled=True)
    blocked = gate.authorize_merge(
        task=task,
        contract=contract,
        head_sha="sha123",
        diff_entries=[DiffPathEntry(status="M", path="backend/tests/test_demo.py")],
        test_file_snapshots={"backend/tests/test_demo.py": (py_before, py_after)},
        reviewer_provider="openai",
        evidence_records=[{"kind": "ci_check", "commit_sha": "sha123", "contract_sha256": digest, "verified": 1}],
    )
    assert blocked.allowed is False
    assert blocked.block_reason == "assertion_relaxation"

    # With exact-SHA owner waiver in governance PR -> allowed
    task["assertion_waiver_sha"] = "sha123"
    task["assertion_waiver_reason"] = "Consolidated duplicate assertions"
    allowed = gate.authorize_merge(
        task=task,
        contract=contract,
        head_sha="sha123",
        diff_entries=[DiffPathEntry(status="M", path="backend/tests/test_demo.py")],
        test_file_snapshots={"backend/tests/test_demo.py": (py_before, py_after)},
        reviewer_provider="openai",
        evidence_records=[{"kind": "ci_check", "commit_sha": "sha123", "contract_sha256": digest, "verified": 1}],
    )
    assert allowed.allowed is True


def test_scrubbed_runner_env_strips_host_secrets() -> None:
    fake_host = {
        "PATH": "/usr/bin",
        "TEMP": "/tmp",
        "AWS_SECRET_ACCESS_KEY": "secret-aws",
        "OPENAI_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghp_secret",
        "GH_TOKEN": "ghp_secret2",
    }
    env = build_scrubbed_env(extra_env={"ANTHROPIC_MODEL": "deepseek-chat"}, base_env=fake_host)
    assert env["PATH"] == "/usr/bin"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["ANTHROPIC_MODEL"] == "deepseek-chat"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


# =====================================================================
# 2. Phase 8c: Durable state machine, outbox, fencing, crash recovery
# =====================================================================


def test_durable_state_machine_blocked_resume_and_outbox(tmp_path: Path) -> None:
    with Store(tmp_path / "ns.db") as store:
        store.migrate()
        _, plan_id, phase_id = _seed_project_plan_phase(store)
        task, contract = store.create_task_with_contract(
            task_id="t-orch",
            phase_id=phase_id,
            plan_id=plan_id,
            title="Durable test",
            branch="feat/t-orch",
            risk_class="governance",
            risk_factors={"impact": 4},
            acceptance_criteria=[{"id": "ac-1", "description": "Works"}],
            allowed_globs=["backend/tests/**"],
            forbidden_globs=[],
            budget={"max_attempts": 4},
            owner_approval_required=True,
        )
        orch = TaskOrchestrator(store)
        lease = orch.acquire_lease("t-orch", worker_pid=999999)
        with pytest.raises(LeaseConflictError):
            orch.acquire_lease("t-orch", worker_pid=123456)

        # Illegal transition rejected
        with pytest.raises(InvalidTransitionError):
            orch.transition("t-orch", "RELEASED", idempotency_key="bad-1", reason="skip")

        # Legal transitions + idempotent replay
        orch.transition("t-orch", "RUNNING", idempotency_key="k1", reason="start")
        orch.transition("t-orch", "RUNNING", idempotency_key="k1", reason="replay")
        assert len(store.list_task_transitions("t-orch")) == 1

        orch.transition("t-orch", "VERIFYING", idempotency_key="k2", reason="pr opened", updates={"head_sha": "head1", "pr_number": 7})
        store.record_evidence(
            task_id="t-orch",
            contract_sha256=contract["contract_sha256"],
            commit_sha="head1",
            kind="ci_check",
            source="circleci:desktop-backend",
            duration_seconds=25.0,
            verified=True,
        )
        orch.transition("t-orch", "REVIEWING", idempotency_key="k3", reason="ci green", updates={"review_sha": "head1"})
        blocked = orch.transition("t-orch", "BLOCKED", idempotency_key="k4", reason="awaiting_owner_approval")
        assert blocked["blocked_from_status"] == "REVIEWING"

        # Resuming from BLOCKED with owner approval advances directly to APPROVED (never restarts coding!)
        resumed = orch.resume_from_blocked("t-orch", current_head_sha="head1", owner_approved_by="umair")
        assert resumed["status"] == "APPROVED"
        assert resumed["owner_approved_sha"] == "head1"

        # Remote outbox reconciliation: open PR, closed PR, ambiguous merge, partial release
        r_open = orch.reconcile_pr_create(
            task_id="t-orch", branch="feat/t-orch", lease_token=lease, existing_prs=[{"number": 7, "state": "OPEN"}]
        )
        assert r_open.action == "adopted_open_pr"

        r_closed = orch.reconcile_pr_create(
            task_id="t-orch", branch="feat/t-closed", lease_token=lease, existing_prs=[{"number": 8, "state": "CLOSED"}]
        )
        assert r_closed.action == "blocked"
        assert r_closed.data["block_reason"] == "closed_pr_exists"

        r_ambig = orch.reconcile_merge_outcome(
            task_id="t-orch",
            pr_number=7,
            approved_sha="head1",
            lease_token=lease,
            pr_state_lookup=lambda: (_ for _ in ()).throw(RuntimeError("network timeout")),
        )
        assert r_ambig.action == "blocked"
        assert r_ambig.data["block_reason"] == "ambiguous_remote_operation"

        r_partial = orch.reconcile_release_for_merge_sha(
            task_id="t-orch",
            merge_sha="merge1",
            expected_asset_names=["NightShift-windows.zip"],
            lease_token=lease,
            existing_releases=[{"tag": "v1.0.0-preview.1", "body": "<!-- nightshift-merge-sha: merge1 -->", "assets": []}],
        )
        assert r_partial.action == "blocked"
        assert r_partial.data["block_reason"] == "partial_release_repair"

        # Network backoff schedule
        assert [network_backoff_delay(i) for i in (1, 2, 3, 4, 5)] == [30, 60, 120, 300, 300]


def test_orphan_running_task_recovery(tmp_path: Path) -> None:
    with Store(tmp_path / "ns.db") as store:
        store.migrate()
        _, plan_id, phase_id = _seed_project_plan_phase(store)
        store.create_task_with_contract(
            task_id="t-crash",
            phase_id=phase_id,
            plan_id=plan_id,
            title="Crash recovery",
            branch="feat/t-crash",
            risk_class="docs_format",
            risk_factors={"impact": 1},
            acceptance_criteria=[],
            allowed_globs=["docs/**"],
            forbidden_globs=[],
            budget={"max_attempts": 4},
        )
        orch = TaskOrchestrator(store)
        orch.acquire_lease("t-crash", worker_pid=55555)
        orch.transition("t-crash", "RUNNING", idempotency_key="run-1", reason="start")

        checkpointed: list[str] = []
        recovered = orch.recover_orphaned_running_tasks(
            is_pid_alive=lambda _pid: False,
            checkpoint_fn=lambda t: checkpointed.append(str(t["id"])),
        )
        assert recovered == ["t-crash"]
        assert checkpointed == ["t-crash"]
        assert store.get_task("t-crash")["lease_token"] is None


# =====================================================================
# 3. Phase 8d: Evidence integrity, screenshots, mechanical PR body
# =====================================================================


def test_evidence_integrity_rejects_zero_duration_stale_and_missing_artifacts(tmp_path: Path) -> None:
    # Zero-duration / compat-mode rejected
    zero_res = validate_ci_evidence(
        expected_commit_sha="sha1",
        required_jobs=["desktop-backend"],
        observed_jobs=[CiJobCheck("desktop-backend", "success", "sha1", "b1", duration_seconds=0.5)],
    )
    assert zero_res.verified is False
    assert zero_res.rejection_reason == "suspicious_zero_duration_check"

    # Missing artifact rejected
    art_res = validate_ci_evidence(
        expected_commit_sha="sha1",
        required_jobs=["desktop-frontend"],
        observed_jobs=[CiJobCheck("desktop-frontend", "success", "sha1", "b2", duration_seconds=45.0, artifacts=[])],
        required_artifacts_by_job={"desktop-frontend": ["ui-screenshots"]},
    )
    assert art_res.verified is False
    assert art_res.rejection_reason == "missing_required_artifact"

    # Valid CI + verified screenshot manifest
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    png_sha = hashlib.sha256(png.read_bytes()).hexdigest()
    manifest = tmp_path / "screenshot-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "build_id": "b2",
                "commit_sha": "sha1",
                "screenshots": [
                    {
                        "file": "shot.png",
                        "viewport_or_device": "1440x900",
                        "scale": 100,
                        "axe_violations": 0,
                        "overflow": False,
                        "sha256": png_sha,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    shot_res = validate_screenshot_manifest(manifest, expected_commit_sha="sha1", expected_build_id="b2")
    assert shot_res.verified is True

    # Mechanical PR body from real data
    body = render_mechanical_pr_body(
        task={"id": "t-1", "title": "Add pipeline", "risk_class": "business_logic", "head_sha": "sha1234567"},
        contract={"contract_sha256": "deadbeef" * 8, "acceptance_criteria_json": json.dumps([{"id": "ac-1", "description": "Pass"}])},
        changed_files=["backend/nightshift/store.py"],
        evidence_records=[{"source": "circleci:desktop-backend", "verified": 1, "duration_seconds": 32.0, "commit_sha": "sha1234567"}],
        screenshots=["shot.png"],
    )
    assert "circleci:desktop-backend" in body
    assert "PASS" in body
    assert "shot.png" in body


# =====================================================================
# 4. Phase 8e: Risk classification, independent review, release smoke/rollback
# =====================================================================


def test_risk_classification_and_independent_reviewer_exclusions(tmp_path: Path) -> None:
    assert compute_risk_score({"impact": 5, "data_sensitivity": 5, "reversibility": 1, "test_coverage": 2, "uncertainty": 4}) >= 3.8

    risk_cls, _, _ = classify_task_risk(
        entries=[DiffPathEntry(status="M", path="backend/nightshift/engine/credentials.py")],
        planned_risk_class="docs_format",
    )
    assert risk_cls == "high_risk"

    # agy and gemini are both google -> neither may review the other
    assert engine_provider("agy") == "google"
    assert engine_provider("gemini") == "google"

    candidates = [
        ReviewerCandidate("agy", "neon", "standard"),
        ReviewerCandidate("gemini", "gemini-2.5", "standard"),
        ReviewerCandidate("codex", "gpt-5", "standard"),
    ]
    chosen, _ = select_independent_reviewer(
        candidates=candidates,
        contributing_providers=["google"],
        risk_class="business_logic",
    )
    assert chosen is not None and chosen.engine_id == "codex"

    # If all providers contributed -> None (never self-review)
    none_chosen, reason = select_independent_reviewer(
        candidates=candidates,
        contributing_providers=["google", "openai"],
        risk_class="business_logic",
    )
    assert none_chosen is None
    assert "no_independent_reviewer" in reason

    # Strict prompt isolation: no PR narrative
    prompt = build_independent_review_prompt(contract_canonical_json='{"task_id":"t-1"}', raw_git_diff="+x = 1")
    assert "FROZEN_CONTRACT_JSON" in prompt
    assert "RAW_GIT_DIFF" in prompt

    # Validated citations: real file:line + omission citing ac-1 accepted; bad line rejected
    sample = tmp_path / "app.py"
    sample.write_text("line1\nline2\nline3\n", encoding="utf-8")
    good_resp = json.dumps(
        {
            "verdict": "CHANGES",
            "findings": [
                {"priority": "P1", "kind": "defect", "file": "app.py", "line": 2, "message": "Null deref"},
                {"priority": "P2", "kind": "omission", "file": "app.py", "ac_id": "ac-1", "message": "Missing edge case"},
            ],
        }
    )
    res_ok = validate_review_response(good_resp, repo_root=tmp_path, contract_ac_ids={"ac-1"})
    assert res_ok.valid is True and res_ok.verdict == "CHANGES" and len(res_ok.findings) == 2

    bad_resp = json.dumps(
        {
            "verdict": "CHANGES",
            "findings": [{"priority": "P1", "kind": "defect", "file": "app.py", "line": 999, "message": "Hallucinated line"}],
        }
    )
    res_bad = validate_review_response(bad_resp, repo_root=tmp_path, contract_ac_ids={"ac-1"})
    assert res_bad.valid is False


def test_release_smoke_fail_closed_and_one_click_rollback(tmp_path: Path) -> None:
    with Store(tmp_path / "ns.db") as store:
        store.migrate()
        gov = ReleaseGovernance(store)

        # Missing smoke procedure fails closed (unavailable)
        unavail = gov.verify_post_release_smoke(
            tag="v1.0.0-preview.2",
            task_id=None,
            repo="owner/ns",
            merge_sha="m2",
            artifact_sha256="abc",
            smoke_command=None,
            smoke_runner=None,
            previous_good_tag="v1.0.0-preview.1",
        )
        assert unavail.status == "unavailable"

        # Prepare archived previous good release and test rollback
        archive_dir = tmp_path / "archive"
        prev_dir = archive_dir / "v1.0.0-preview.1"
        prev_dir.mkdir(parents=True)
        (prev_dir / "NightShift-windows.zip").write_bytes(b"good-zip")
        (prev_dir / "SHA256SUMS.txt").write_text("sha  NightShift-windows.zip\n", encoding="utf-8")

        out_dir = tmp_path / "output"
        rb = gov.rollback_release(faulty_tag="v1.0.0-preview.2", archive_dir=archive_dir, output_dir=out_dir)
        assert rb["restoredTag"] == "v1.0.0-preview.1"
        assert (out_dir / "NightShift-windows.zip").read_bytes() == b"good-zip"
        assert store.get_release_verification("v1.0.0-preview.2")["smoke_status"] == "rolled_back"


# =====================================================================
# 5. Phase 8f: Triage, economics graduation/demotion, safeguard-first router, budgets
# =====================================================================


def test_triage_economics_graduation_and_safeguard_first_router(tmp_path: Path) -> None:
    # Triage <= 10 lines with cause + file + fix
    triage = summarize_ci_failure_triage("FAILED backend/tests/test_store.py:42\nAssertionError: expected 5\nFix: bump version")
    assert len(triage.lines) <= 10
    assert "backend/tests/test_store.py:42" in triage.formatted()

    with Store(tmp_path / "ns.db") as store:
        store.migrate()
        _, plan_id, phase_id = _seed_project_plan_phase(store)
        store.create_task_with_contract(
            task_id="t-econ",
            phase_id=phase_id,
            plan_id=plan_id,
            title="Econ task",
            branch="feat/t-econ",
            risk_class="docs_format",
            risk_factors={"impact": 1},
            acceptance_criteria=[],
            allowed_globs=["docs/**"],
            forbidden_globs=[],
            budget={"max_attempts": 4},
        )
        econ = PipelineEconomics(store)

        # Record 10 accepted tasks for Economy model qwen -> graduates
        for i in range(10):
            tid = f"t-bench-{i}"
            store.create_task_with_contract(
                task_id=tid,
                phase_id=phase_id,
                plan_id=plan_id,
                title=f"Bench {i}",
                branch=f"feat/bench-{i}",
                risk_class="docs_format",
                risk_factors={"impact": 1},
                acceptance_criteria=[],
                allowed_globs=["docs/**"],
                forbidden_globs=[],
                budget={"max_attempts": 4},
            )
            store.record_pipeline_ledger(
                task_id=tid,
                role="coder",
                engine_id="qwen",
                provider="alibaba",
                model="qwen3-coder",
                risk_class="docs_format",
                outcome="accepted",
                est_token_cost_usd=0.0,
                ci_minutes=1.0,
                wall_seconds=60.0,
            )

        score = econ.recompute_model_score(
            engine_id="qwen",
            model="qwen3-coder",
            role="coder",
            risk_class="docs_format",
            incumbent_standard_cost_usd=0.25,
            is_economy=True,
        )
        assert score["benchmark_graduated"] == 1
        assert score["sample_tasks"] == 10

        # Safeguard-first router: owner override violating provider independence -> BLOCKED
        router = PipelineRouter(store, BudgetGuard(store, BudgetLimits(free_only_mode=True)))
        engines = [
            RouterEngineSpec("qwen", "qwen3-coder", tier="economy", is_paid=False),
            RouterEngineSpec("gemini", "gemini-2.5", tier="free_standard", is_paid=False),
            RouterEngineSpec("codex", "gpt-5", tier="free_standard", is_paid=False),
            RouterEngineSpec("claude", "sonnet", tier="gold", is_paid=True),
        ]

        blocked_override = router.route(
            task_id="t-econ",
            role="reviewer",
            risk_class="business_logic",
            engines=engines,
            contributing_providers=["google"],
            owner_override="gemini",
        )
        assert blocked_override.blocked is True
        assert "contributing_providers" in blocked_override.reason

        # Free-only mode blocks paid engine override
        blocked_paid = router.route(
            task_id="t-econ",
            role="coder",
            risk_class="business_logic",
            engines=engines,
            owner_override="claude",
        )
        assert blocked_paid.blocked is True
        assert "free_only_mode" in blocked_paid.reason

        # Cold-start bootstrap for Standard/Gold does NOT deadlock
        cold = router.route(
            task_id="t-econ",
            role="coder",
            risk_class="high_risk",
            engines=engines,
        )
        assert cold.blocked is False
        assert cold.chosen is not None
        assert cold.chosen.engine_id in ("gemini", "codex")

        # Graduated Economy model wins on docs_format
        grad_pick = router.route(
            task_id="t-econ",
            role="coder",
            risk_class="docs_format",
            engines=engines,
        )
        assert grad_pick.blocked is False
        assert grad_pick.chosen is not None
        assert grad_pick.chosen.engine_id == "qwen"


def test_pipeline_backend_module_bridge_rpc(tmp_path: Path) -> None:
    with Store(tmp_path / "ns.db") as store:
        store.migrate()
        events = EventBus(store=store)
        registry = ModuleRegistry()
        discovered = registry.discover()
        assert "pipeline" in discovered

        bridge = BridgeAPI(store=store, events=events, registry=registry)
        res = bridge.call("pipeline.getState")
        assert res["ok"] is True
        state = res["data"]
        assert state["summary"]["freeOnlyMode"] is True
        assert len(state["stageMaturity"]) == 9
        triage_res = bridge.call("pipeline.triagePreview", {"log": "ERROR src/main.py:12 failed"})
        assert triage_res["ok"] is True
        assert "src/main.py:12" in triage_res["data"]["summary"]
