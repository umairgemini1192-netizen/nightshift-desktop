"""Phase 8e tests: Risk-based review & release governance (Amendments C, G, H).

Covers docs/AGENT_PIPELINE.md sections 1, 2, 6.1, 9.2, 9.3, and 12.1 Phase 8e:
- Mechanical 5-factor risk classifier with upward-only path/content escalation
- Provider-independent reviewer excluding ALL contributing providers (coders + fixers + handoffs)
- Strict input isolation (raw diff + frozen contract only, never PR narrative)
- Validated citations (changed diff lines, unchanged repo callers, structured omission findings)
- Single schema/citation retry and fail-closed invalid review handling
- Owner-approval gates for high_risk/governance vs conditional auto-merge for eligible classes
- Mandatory post-release launch/smoke test (fail-closed when unavailable or failing)
- One-click rollback preserving tag history, restoring verified artifacts/checksums, optional revert PR
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nightshift.bridge import BridgeAPI
from nightshift.engine.ci import CiPipelineManager
from nightshift.engine.deck import generate_deck_data
from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.pipeline_contracts import (
    DiffPathEntry,
    MergeGate,
    canonical_contract_json,
    provider_for_engine,
)
from nightshift.engine.pipeline_evidence import CiArtifact, CiJobCheck
from nightshift.engine.pipeline_review import (
    build_isolated_review_prompt,
    classify_task_risk,
    compute_risk_score,
    pack_raw_diff_for_review,
    parse_and_validate_review_output,
    select_independent_reviewer,
)
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.release import (
    DESKTOP_SELFTEST_SMOKE,
    ReleaseManager,
    SmokeTestResult,
    compute_sha256,
)
from nightshift.engine.scheduler import Scheduler
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
from nightshift.engine.vcs import FakeGitGh
from nightshift.engine.verify import RiskAssessment
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.base import RunnerResult
from nightshift.runners.fake import FakeRunner
from nightshift.store import Store


def _ok_login_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    return (0, '{"loggedIn": true}\nLogged in to github.com\nOK', "", False)


def _approve_json(summary: str = "All checks and contract criteria satisfied.") -> str:
    return json.dumps({"verdict": "APPROVE", "summary": summary, "findings": []})


def _make_valid_jobs(commit_sha: str, *, branch: str = "feat/p8e") -> list[CiJobCheck]:
    return [
        CiJobCheck(
            name="desktop-gates",
            status="success",
            commit_sha=commit_sha,
            build_id="b-1",
            duration_seconds=8.0,
            is_gate_job=True,
            branch=branch,
        ),
        CiJobCheck(
            name="desktop-backend",
            status="success",
            commit_sha=commit_sha,
            build_id="b-2",
            duration_seconds=25.0,
            branch=branch,
        ),
        CiJobCheck(
            name="desktop-frontend",
            status="success",
            commit_sha=commit_sha,
            build_id="b-3",
            duration_seconds=45.0,
            artifacts=[CiArtifact(path="ui-screenshots/screenshot-manifest.json", size_bytes=512, build_id="b-3")],
            branch=branch,
        ),
        CiJobCheck(
            name="desktop-package",
            status="success",
            commit_sha=commit_sha,
            build_id="b-4",
            duration_seconds=90.0,
            artifacts=[CiArtifact(path="nightshift-windows/NightShift-windows.zip", size_bytes=1024, build_id="b-4")],
            branch=branch,
        ),
    ]


def _seed_task(
    store: Store,
    *,
    task_id: str = "t-8e",
    branch: str = "feat/p8e",
    risk_class: str = "business_logic",
    allowed_globs: list[str] | None = None,
    acceptance_criteria: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    store.create_project("proj-1", "NightShift", "owner/repo", "/tmp/repo")
    store.create_plan("plan-1", "proj-1", "Plan 8e")
    store.upsert_phase("p1", "plan-1", "Phase 8e", branch)
    return store.create_task_with_contract(
        task_id=task_id,
        phase_id="p1",
        plan_id="plan-1",
        title="Phase 8e Task",
        branch=branch,
        risk_class=risk_class,
        allowed_globs=allowed_globs or ["src/**", "docs/**", "frontend/**", "backend/**"],
        acceptance_criteria=acceptance_criteria
        or [
            {"id": "ac-1", "description": "Implements risk classification"},
            {"id": "ac-2", "description": "Enforces provider independence"},
        ],
    )


# ---------------------------------------------------------------------------
# 1. Risk Classifier (Amendment C)
# ---------------------------------------------------------------------------


def test_compute_risk_score_weighted_formula_and_clamping() -> None:
    low = compute_risk_score(
        {"impact": 1, "data_sensitivity": 1, "reversibility": 5, "test_coverage": 5, "uncertainty": 1}
    )
    assert low == 1.0

    high = compute_risk_score(
        {"impact": 5, "data_sensitivity": 5, "reversibility": 1, "test_coverage": 1, "uncertainty": 5}
    )
    assert high == 5.0

    # Clamping out-of-range values
    clamped = compute_risk_score(
        {"impact": 99, "data_sensitivity": -5, "reversibility": 3, "test_coverage": 3, "uncertainty": 2}
    )
    expected = round(0.30 * 5 + 0.25 * 1 + 0.20 * 3 + 0.15 * 3 + 0.10 * 2, 3)
    assert clamped == expected


def test_classify_task_risk_all_five_classes_and_upward_only_escalation() -> None:
    # 1. docs_format
    docs_res = classify_task_risk([DiffPathEntry("M", "docs/README.md")], "+Updated docs", base_risk_class="docs_format")
    assert docs_res.risk_class == "docs_format"
    assert docs_res.auto_merge_eligible is True
    assert docs_res.requires_independent_review is False

    # 2. ui_cosmetic
    ui_res = classify_task_risk(
        [DiffPathEntry("M", "frontend/src/modules/mission-control/Page.tsx")],
        "+<GlassCard />",
        base_risk_class="docs_format",
    )
    assert ui_res.risk_class == "ui_cosmetic"
    assert ui_res.escalated_from == "docs_format"
    assert ui_res.requires_screenshots is True
    assert ui_res.auto_merge_eligible is True

    # 3. business_logic
    logic_res = classify_task_risk(
        [DiffPathEntry("M", "backend/nightshift/engine/planner.py")],
        "+def sort_dag(): pass",
        base_risk_class="docs_format",
    )
    assert logic_res.risk_class == "business_logic"
    assert logic_res.requires_independent_review is True
    assert logic_res.requires_owner_approval is False

    # 4. high_risk via path signal (auth/credentials) and via diff content (ALTER TABLE)
    auth_res = classify_task_risk(
        [DiffPathEntry("M", "backend/nightshift/engine/credentials.py")],
        "+def store_token(): pass",
        base_risk_class="business_logic",
    )
    assert auth_res.risk_class == "high_risk"
    assert auth_res.requires_strong_reviewer is True
    assert auth_res.requires_owner_approval is True
    assert auth_res.auto_merge_eligible is False

    sql_res = classify_task_risk(
        [DiffPathEntry("M", "backend/nightshift/store.py")],
        "+ALTER TABLE projects ADD COLUMN secret_flag TEXT",
        base_risk_class="business_logic",
    )
    assert sql_res.risk_class == "high_risk"

    # 5. governance via rename/delete/case-variant protected path
    gov_res = classify_task_risk(
        [DiffPathEntry("R", old_path="backend/tests/CONFTEST.PY", path="src/moved.py")],
        "+pass",
        base_risk_class="docs_format",
        case_fold=True,
    )
    assert gov_res.risk_class == "governance"
    assert gov_res.requires_owner_approval is True
    assert gov_res.auto_merge_eligible is False

    # 6. Never downgrades below base_risk_class
    no_downgrade = classify_task_risk(
        [DiffPathEntry("M", "docs/notes.md")],
        "+typo fix",
        base_risk_class="high_risk",
    )
    assert no_downgrade.risk_class == "high_risk"
    assert no_downgrade.escalated_from is None


# ---------------------------------------------------------------------------
# 2. Provider-Independent Reviewer & Citation Validation (Amendment G)
# ---------------------------------------------------------------------------


def test_provider_independence_excludes_all_contributing_providers_and_never_self_fallbacks() -> None:
    runners = {
        "agy": FakeRunner.success("agy"),
        "gemini": FakeRunner.success("gemini"),
        "codex": FakeRunner.success("codex"),
        "claude": FakeRunner.success("claude"),
        "qwen": FakeRunner.success("qwen"),
    }

    # agy (google) contributed -> gemini (also google) MUST be excluded; codex (openai) chosen
    chosen, _, _ = select_independent_reviewer(
        contributing_providers={"google"},
        risk_class="business_logic",
        runners={"agy": runners["agy"], "gemini": runners["gemini"]},
        is_available=lambda _: True,
    )
    assert chosen is None  # NEVER falls back to agy or gemini!

    # Coder (google) + fixer (openai) both contributed -> claude (anthropic) chosen
    chosen2, _, _ = select_independent_reviewer(
        contributing_providers={"google", "openai"},
        risk_class="business_logic",
        runners=runners,
        is_available=lambda _: True,
    )
    assert chosen2 == "claude"
    assert provider_for_engine(chosen2) == "anthropic"

    # High-risk rejects economy reviewer even if graduated
    chosen3, _, reason3 = select_independent_reviewer(
        contributing_providers={"google", "openai", "anthropic"},
        risk_class="high_risk",
        runners=runners,
        is_available=lambda _: True,
        graduated_economy_engines={"qwen"},
    )
    assert chosen3 is None
    assert "not allowed for high_risk" in reason3

    # Business logic allows economy reviewer ONLY when benchmark-graduated
    chosen4_ungrad, _, _ = select_independent_reviewer(
        contributing_providers={"google", "openai", "anthropic"},
        risk_class="business_logic",
        runners=runners,
        is_available=lambda _: True,
        graduated_economy_engines=set(),
    )
    assert chosen4_ungrad is None

    chosen4_grad, _, _ = select_independent_reviewer(
        contributing_providers={"google", "openai", "anthropic"},
        risk_class="business_logic",
        runners=runners,
        is_available=lambda _: True,
        graduated_economy_engines={"qwen"},
    )
    assert chosen4_grad == "qwen"


def test_isolated_review_prompt_excludes_pr_narrative_and_packs_whole_hunks() -> None:
    app_diff = "diff --git a/src/app.py b/src/app.py\n@@ -1,2 +1,3 @@\n def run():\n+    return 1\n"
    test_diff = "diff --git a/tests/test_app.py b/tests/test_app.py\n@@ -1,2 +1,3 @@\n def test_run():\n+    assert True\n"
    packed = pack_raw_diff_for_review(test_diff + "\n" + app_diff, max_chars=len(app_diff) + 40)
    # App code prioritized before tests; whole hunk preserved
    assert "src/app.py" in packed
    assert "@@ -1,2 +1,3 @@" in packed

    contract_json = canonical_contract_json(
        {
            "task_id": "t1",
            "title": "Add widget",
            "acceptance_criteria": [{"id": "ac-1", "description": "Works"}],
        }
    )
    prompt = build_isolated_review_prompt(
        contract_canonical_json=contract_json,
        raw_diff=app_diff,
        head_sha="abc1234",
    )
    assert "FROZEN TASK CONTRACT (JSON):" in prompt
    assert "RAW GIT DIFF:" in prompt
    assert "Do NOT modify files, run git mutations, or rely on any PR description" in prompt
    assert "src/app.py" in prompt


def test_parse_and_validate_review_output_citations_and_omissions(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "feature.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (wt / "src" / "caller.py").write_text("import feature\nfeature.run()\n", encoding="utf-8")

    contract = {
        "acceptance_criteria_json": json.dumps(
            [{"id": "ac-1", "description": "Handle empty input"}, {"id": "ac-2", "description": "Log audit event"}]
        )
    }

    # 1. Valid defect on changed file + valid defect on unchanged caller + valid omission
    valid_payload = json.dumps(
        {
            "verdict": "CHANGES",
            "summary": "Found one P1 defect and one omission.",
            "findings": [
                {
                    "priority": "P1",
                    "kind": "defect",
                    "file": "src/caller.py",
                    "line": 2,
                    "message": "Unchanged caller breaks with new signature.",
                },
                {
                    "priority": "P2",
                    "kind": "omission",
                    "file": "src/feature.py",
                    "line": 1,
                    "acceptance_criteria_id": "ac-2",
                    "message": "Audit logging requirement not yet wired.",
                },
            ],
        }
    )
    res_ok = parse_and_validate_review_output(valid_payload, worktree_dir=wt, contract=contract)
    assert res_ok.valid is True
    assert res_ok.verdict == "CHANGES"
    assert len(res_ok.p1_findings) == 1
    assert len(res_ok.p2_findings) == 1

    # 2. Invalid line out of bounds
    bad_line = json.dumps(
        {
            "verdict": "CHANGES",
            "summary": "Bad line",
            "findings": [{"priority": "P1", "kind": "defect", "file": "src/feature.py", "line": 999, "message": "Boom"}],
        }
    )
    res_bad_line = parse_and_validate_review_output(bad_line, worktree_dir=wt, contract=contract)
    assert res_bad_line.valid is False
    assert any("only 3 line(s)" in e for e in res_bad_line.errors)

    # 3. Unknown acceptance_criteria_id in omission
    bad_ac = json.dumps(
        {
            "verdict": "CHANGES",
            "summary": "Bad AC",
            "findings": [
                {
                    "priority": "P1",
                    "kind": "omission",
                    "file": "src/feature.py",
                    "acceptance_criteria_id": "ac-99",
                    "message": "Missing",
                }
            ],
        }
    )
    res_bad_ac = parse_and_validate_review_output(bad_ac, worktree_dir=wt, contract=contract)
    assert res_bad_ac.valid is False
    assert any("unknown acceptance_criteria_id" in e for e in res_bad_ac.errors)

    # 4. APPROVE with P1 finding is rejected
    inconsistent = json.dumps(
        {
            "verdict": "APPROVE",
            "summary": "Approve with P1",
            "findings": [{"priority": "P1", "kind": "defect", "file": "src/feature.py", "line": 1, "message": "Bug"}],
        }
    )
    res_inc = parse_and_validate_review_output(inconsistent, worktree_dir=wt, contract=contract)
    assert res_inc.valid is False


@pytest.mark.asyncio
async def test_ci_review_retries_once_on_invalid_citations_and_blocks_if_still_invalid(tmp_path: Path) -> None:
    with Store(tmp_path / "rev_retry.db") as store:
        store.migrate()
        _, contract = _seed_task(store, task_id="t-retry", branch="feat/retry")
        vcs = FakeGitGh()
        vcs.add_pr("feat/retry", number=201, head_sha="sha_201")
        wt = tmp_path / "wt"
        (wt / "src").mkdir(parents=True)
        (wt / "src" / "main.ts").write_text("export const x = 1;\n", encoding="utf-8")
        vcs.worktrees["feat/retry"] = wt
        vcs.changed_files["feat/retry"] = ["src/main.ts"]

        # First call returns invalid citation, second returns valid APPROVE JSON -> succeeds after 1 retry
        codex_retry_ok = FakeRunner(
            name="codex",
            responses=[
                RunnerResult(rc=0, stdout='{"verdict":"CHANGES","findings":[{"priority":"P1","file":"nope.py","line":1,"message":"bad"}]}', stderr=""),
                RunnerResult(rc=0, stdout=_approve_json(), stderr=""),
            ],
        )
        ci_mgr = CiPipelineManager(vcs, store, Scheduler(store, QuotaLedger(store)), {"agy": FakeRunner.success("agy"), "codex": codex_retry_ok})
        res = await ci_mgr.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="P1",
            branch="feat/retry",
            pr_number=201,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
            task_contract=contract,
            contributing_providers={"google"},
        )
        assert res.passed is True
        assert res.reviewer_engine == "codex"
        assert res.reviewer_provider == "openai"
        assert len(codex_retry_ok.calls) == 2

        # Both calls return malformed output -> fail-closed invalid_review_output
        codex_always_bad = FakeRunner(
            name="codex",
            responses=[
                RunnerResult(rc=0, stdout="not json", stderr=""),
                RunnerResult(rc=0, stdout="still not json", stderr=""),
            ],
        )
        ci_mgr2 = CiPipelineManager(vcs, store, Scheduler(store, QuotaLedger(store)), {"agy": FakeRunner.success("agy"), "codex": codex_always_bad})
        res2 = await ci_mgr2.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="P1",
            branch="feat/retry",
            pr_number=201,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
            task_contract=contract,
            contributing_providers={"google"},
        )
        assert res2.passed is False
        assert res2.block_reason == "invalid_review_output"


@pytest.mark.asyncio
async def test_ci_review_fixer_provider_excluded_from_re_review(tmp_path: Path) -> None:
    """If agy (google) coded and codex (openai) fixed a P1, re-review MUST exclude both google and openai."""
    with Store(tmp_path / "rev_fixer.db") as store:
        store.migrate()
        _, contract = _seed_task(store, task_id="t-fixer", branch="feat/fixer")
        vcs = FakeGitGh()
        vcs.add_pr("feat/fixer", number=202, head_sha="sha_202")
        wt = tmp_path / "wt"
        (wt / "src").mkdir(parents=True)
        (wt / "src" / "main.ts").write_text("const a = 1;\nconst b = 2;\n", encoding="utf-8")
        vcs.worktrees["feat/fixer"] = wt
        vcs.changed_files["feat/fixer"] = ["src/main.ts"]

        # Priority list puts codex first for fixes so codex becomes a contributing provider during fix round
        store.set_priority_list([{"position": 1, "engine": "codex", "model": "o3-mini", "effort": "medium"}])

        p1_json = json.dumps(
            {
                "verdict": "CHANGES",
                "summary": "Null guard missing",
                "findings": [{"priority": "P1", "kind": "defect", "file": "src/main.ts", "line": 1, "message": "Fix a"}],
            }
        )
        codex_runner = FakeRunner(
            name="codex",
            responses=[
                RunnerResult(rc=0, stdout=p1_json, stderr=""),  # round 1 review
                RunnerResult(rc=0, stdout="fixed", stderr=""),  # fix round
            ],
        )
        claude_runner = FakeRunner(name="claude", responses=[RunnerResult(rc=0, stdout=_approve_json(), stderr="")])
        runners = {"agy": FakeRunner.success("agy"), "codex": codex_runner, "claude": claude_runner}

        ci_mgr = CiPipelineManager(vcs, store, Scheduler(store, QuotaLedger(store)), runners)
        res = await ci_mgr.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="P1",
            branch="feat/fixer",
            pr_number=202,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
            task_contract=contract,
            contributing_providers={"google"},
        )
        assert res.passed is True
        # Re-review was performed by claude (anthropic), NOT codex (openai) or agy (google)
        assert res.reviewer_engine == "claude"
        assert res.reviewer_provider == "anthropic"
        assert "codex" in res.contributing_engines


# ---------------------------------------------------------------------------
# 3. MergeGate Risk Policies, Owner Approval & Conditional Auto-Merge
# ---------------------------------------------------------------------------


def test_merge_gate_conditional_auto_merge_and_owner_approval_policies(tmp_path: Path) -> None:
    with Store(tmp_path / "gate_8e.db") as store:
        store.migrate()
        gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=True)

        # 1. docs_format: auto-merges without independent reviewer or owner approval
        t_docs, c_docs = _seed_task(store, task_id="t-docs", branch="feat/docs", risk_class="docs_format")
        res_docs = gate.authorize_merge(
            task=t_docs,
            contract=c_docs,
            head_sha="sha_docs",
            diff_entries=[DiffPathEntry("M", "docs/GUIDE.md")],
            diff_content="+New docs line",
        )
        assert res_docs.allowed is True
        assert res_docs.effective_risk_class == "docs_format"

        # 2. ui_cosmetic: blocks without verified screenshots, allows with verified screenshots
        t_ui, c_ui = store.create_task_with_contract(
            task_id="t-ui",
            phase_id="p1",
            plan_id="plan-1",
            title="UI tweak",
            branch="feat/ui",
            risk_class="ui_cosmetic",
            allowed_globs=["frontend/**"],
        )
        store.record_evidence(
            task_id="t-ui",
            contract_sha256=c_ui["contract_sha256"],
            commit_sha="sha_ui",
            kind="ci_check",
            source="circleci:desktop-frontend",
            build_id="b-10",
            duration_seconds=30.0,
            metadata={"job_name": "desktop-frontend"},
            verified=True,
        )
        cosmetic_diff = (
            "diff --git a/frontend/src/modules/mission-control/Page.tsx b/frontend/src/modules/mission-control/Page.tsx\n"
            "@@ -1,2 +1,2 @@\n"
            '-<GlassCard className="p-4">\n'
            '+<GlassCard className="p-6">\n'
        )
        res_ui_no_shot = gate.authorize_merge(
            task=t_ui,
            contract=c_ui,
            head_sha="sha_ui",
            diff_entries=[DiffPathEntry("M", "frontend/src/modules/mission-control/Page.tsx")],
            diff_content=cosmetic_diff,
            evidence_records=store.list_evidence_records("t-ui", commit_sha="sha_ui"),
        )
        assert res_ui_no_shot.allowed is False
        assert res_ui_no_shot.block_reason == "missing_verified_screenshots"

        store.record_evidence(
            task_id="t-ui",
            contract_sha256=c_ui["contract_sha256"],
            commit_sha="sha_ui",
            kind="screenshot",
            source="playwright:manifest",
            build_id="b-10",
            metadata={"renderer": "playwright-mockbridge"},
            verified=True,
        )
        res_ui_ok = gate.authorize_merge(
            task=t_ui,
            contract=c_ui,
            head_sha="sha_ui",
            diff_entries=[DiffPathEntry("M", "frontend/src/modules/mission-control/Page.tsx")],
            diff_content=cosmetic_diff,
            evidence_records=store.list_evidence_records("t-ui", commit_sha="sha_ui"),
        )
        assert res_ui_ok.allowed is True

        # 3. high_risk escalated from docs_format via diff -> requires strong reviewer AND owner approval
        t_hr, c_hr = store.create_task_with_contract(
            task_id="t-hr",
            phase_id="p1",
            plan_id="plan-1",
            title="Sneaky auth edit",
            branch="feat/hr",
            risk_class="docs_format",
            allowed_globs=["backend/**"],
        )
        store.record_task_contributing_provider("t-hr", "google")
        t_hr = store.get_task("t-hr")
        assert t_hr is not None

        # Economy reviewer rejected for high_risk
        res_hr_econ = gate.authorize_merge(
            task=t_hr,
            contract=c_hr,
            head_sha="sha_hr",
            diff_entries=[DiffPathEntry("M", "backend/nightshift/engine/credentials.py")],
            reviewer_provider="alibaba",
            reviewer_engine="qwen",
            reviewer_tier="economy",
        )
        assert res_hr_econ.allowed is False
        assert res_hr_econ.block_reason == "reviewer_tier_insufficient"
        assert res_hr_econ.effective_risk_class == "high_risk"

        # Strong reviewer passes review check, but still blocks for owner approval even with autonomous_merge_enabled=True!
        res_hr_owner = gate.authorize_merge(
            task=t_hr,
            contract=c_hr,
            head_sha="sha_hr",
            diff_entries=[DiffPathEntry("M", "backend/nightshift/engine/credentials.py")],
            reviewer_provider="openai",
            reviewer_engine="codex",
            reviewer_tier="standard",
        )
        assert res_hr_owner.allowed is False
        assert res_hr_owner.block_reason == "awaiting_owner_approval"

        # Stale SHA owner approval still blocked
        store.record_task_owner_approval("t-hr", "owner", "sha_old")
        res_hr_stale = gate.authorize_merge(
            task=store.get_task("t-hr") or t_hr,
            contract=c_hr,
            head_sha="sha_hr",
            diff_entries=[DiffPathEntry("M", "backend/nightshift/engine/credentials.py")],
            reviewer_provider="openai",
            reviewer_engine="codex",
            reviewer_tier="standard",
        )
        assert res_hr_stale.allowed is False
        assert res_hr_stale.block_reason == "awaiting_owner_approval"

        # Exact-SHA owner approval succeeds
        store.record_task_owner_approval("t-hr", "owner", "sha_hr")
        res_hr_ok = gate.authorize_merge(
            task=store.get_task("t-hr") or t_hr,
            contract=c_hr,
            head_sha="sha_hr",
            diff_entries=[DiffPathEntry("M", "backend/nightshift/engine/credentials.py")],
            reviewer_provider="openai",
            reviewer_engine="codex",
            reviewer_tier="standard",
        )
        assert res_hr_ok.allowed is True


# ---------------------------------------------------------------------------
# 4. Mandatory Release Smoke Test & One-Click Rollback (Amendment H)
# ---------------------------------------------------------------------------


def test_release_smoke_test_fail_closed_and_rollback(tmp_path: Path) -> None:
    with Store(tmp_path / "rel_8e.db") as store:
        store.migrate()
        vcs = FakeGitGh()
        out_dir = tmp_path / "dist"
        out_dir.mkdir()

        # 1. Publish v1 (good release) with passing smoke test
        def pass_smoke(cmd: str, art: Path, tag: str, timeout: int) -> SmokeTestResult:
            return SmokeTestResult(passed=True, status="passed", detail=f"Smoke OK for {tag}", command=cmd, verified_at="2026-11-28T01:00:00Z")

        rel_mgr_ok = ReleaseManager(vcs, smoke_runner=pass_smoke)
        vcs.artifacts_to_create = ["NightShift-windows.zip"]
        tag1, files1 = rel_mgr_ok.publish_verified_release(
            "owner/repo",
            out_dir,
            "Good release",
            merge_sha="sha_merge_good",
            artifact_glob="*.zip",
            run_id=10,
        )
        art1 = next(f for f in files1 if f.name == "NightShift-windows.zip")
        good_sha = compute_sha256(art1)
        s1 = rel_mgr_ok.verify_and_record_release(
            store=store,
            repo="owner/repo",
            tag=tag1,
            merge_sha="sha_merge_good",
            artifact_path=art1,
            smoke_command=DESKTOP_SELFTEST_SMOKE,
        )
        assert s1.passed is True
        rec1 = store.get_release_verification("owner/repo", tag1)
        assert rec1 is not None and rec1["smoke_status"] == "passed"

        # 2. Unconfigured smoke_command fails closed as unavailable
        s_unavail = ReleaseManager(vcs).run_launch_smoke_test(
            artifact_path=art1,
            tag="v1.0.0-preview.unavail",
            smoke_command="",
        )
        assert s_unavail.passed is False
        assert s_unavail.status == "unavailable"

        # 3. Publish v2 (faulty release with different bytes) whose smoke test fails
        def fail_smoke(cmd: str, art: Path, tag: str, timeout: int) -> SmokeTestResult:
            return SmokeTestResult(passed=False, status="failed", detail="Crash on startup (rc=1)", command=cmd)

        rel_mgr_bad = ReleaseManager(vcs, smoke_runner=fail_smoke)
        bad_tmp = tmp_path / "bad_tmp"
        bad_tmp.mkdir()
        bad_zip = bad_tmp / "NightShift-windows.zip"
        bad_zip.write_bytes(b"PK\x03\x04broken_v2_payload")
        tag2, files2 = rel_mgr_bad.publish_verified_release(
            "owner/repo",
            out_dir,
            "Faulty release",
            merge_sha="sha_merge_bad",
            artifact_glob="*.zip",
            run_id=11,
            prepared_artifacts=[bad_zip],
            temp_dir=bad_tmp,
        )
        art2 = next(f for f in files2 if f.name == "NightShift-windows.zip")
        assert compute_sha256(art2) != good_sha

        s2 = rel_mgr_bad.verify_and_record_release(
            store=store,
            repo="owner/repo",
            tag=tag2,
            merge_sha="sha_merge_bad",
            artifact_path=art2,
            smoke_command=DESKTOP_SELFTEST_SMOKE,
        )
        assert s2.passed is False
        rec2 = store.get_release_verification("owner/repo", tag2)
        assert rec2 is not None
        assert rec2["smoke_status"] == "failed"
        assert rec2["previous_good_tag"] == tag1

        # 4. One-click rollback restores tag1's verified artifact + checksums, appends warning to tag2, opens revert PR
        rb = rel_mgr_bad.rollback_release(
            store=store,
            repo="owner/repo",
            faulty_tag=tag2,
            output_dir=out_dir,
            open_revert_pr=True,
            local_repo=tmp_path,
        )
        assert rb.ok is True
        assert rb.restored_tag == tag1
        assert rb.revert_pr_created is True
        # Output dir now contains tag1's verified bytes again!
        assert compute_sha256(out_dir / "NightShift-windows.zip") == good_sha
        assert (out_dir / f"{tag1}-SHA256SUMS.txt").exists()
        # Faulty release tag still exists (never deleted), with superseded warning appended
        faulty_gh = next(r for r in vcs.releases if r["tag"] == tag2)
        assert "SUPERSEDED / ROLLED BACK" in faulty_gh["body"]
        assert tag1 in faulty_gh["body"]
        # DB marks tag2 as rolled_back
        rec2_after = store.get_release_verification("owner/repo", tag2)
        assert rec2_after is not None and rec2_after["smoke_status"] == "rolled_back"
        assert rec2_after["rolled_back_at"] is not None
        # Revert PR was created on main for sha_merge_bad
        assert any(r["commit_sha"] == "sha_merge_bad" for r in vcs.reverts)


def test_desktop_zip_selftest_fails_closed_when_exe_missing(tmp_path: Path) -> None:
    bad_zip = tmp_path / "NightShift-windows.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("README.txt", "no exe here")
    res = ReleaseManager(FakeGitGh()).run_launch_smoke_test(
        artifact_path=bad_zip,
        tag="v1.0.0-preview.1",
        smoke_command=DESKTOP_SELFTEST_SMOKE,
    )
    assert res.passed is False
    assert res.status == "failed"
    assert "NightShift.exe not found" in res.detail


# ---------------------------------------------------------------------------
# 5. End-to-End EngineStateMachine + Bridge + Deck Integration (Phase 8e)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_engine_conditional_auto_merge_and_mandatory_smoke_gate(tmp_path: Path) -> None:
    """End-to-end Phase 8e flow:
    1. Conditional auto-merge enabled via setting -> business_logic merges with independent reviewer.
    2. Missing smoke_command blocks MERGED -> BLOCKED (smoke_test_unavailable), NEVER RELEASED.
    3. Configuring smoke_command + passing smoke_runner completes MERGED -> RELEASED.
    """
    with Store(tmp_path / "e2e_8e.db") as store:
        store.migrate()
        store.set_setting("pipeline.conditionalAutoMerge", "1")
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()
        vcs.artifacts_to_create = ["NightShift-windows.zip"]

        def on_code(prompt: str, wd: Path, model: str | None, effort: str) -> None:
            (wd / "src").mkdir(parents=True, exist_ok=True)
            (wd / "src" / "feature.py").write_text("def ok():\n    return True\n", encoding="utf-8")
            vcs.add_pr("feat/p8e-e2e", number=808, head_sha="sha_808")
            vcs.changed_files["feat/p8e-e2e"] = ["src/feature.py"]
            vcs.diffs["feat/p8e-e2e"] = "diff --git a/src/feature.py b/src/feature.py\n@@ -0,0 +1,2 @@\n+def ok():\n+    return True\n"
            vcs.commit_ci_checks["sha_808"] = _make_valid_jobs("sha_808", branch="feat/p8e-e2e")
            vcs.commit_ci_checks["sha_merge_123"] = _make_valid_jobs("sha_merge_123", branch="main")

        runners = {
            "agy": FakeRunner(name="agy", on_call=on_code),
            "codex": FakeRunner(name="codex", responses=[RunnerResult(rc=0, stdout=_approve_json(), stderr="")]),
        }
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)

        # Phase 1: smoke_command NOT configured -> merges cleanly, publishes release, then BLOCKS on smoke_test_unavailable
        engine_no_smoke = EngineStateMachine(store, events, quota, vcs=vcs, runners=runners, login_checker=logins)
        assert engine_no_smoke.merge_gate.autonomous_merge_enabled is True

        project_no_smoke = ProjectConfig(
            id="proj_8e",
            name="NightShift 8e",
            repo="owner/repo",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "out",
            artifact_glob="*.zip",
            smoke_command="",
        )
        project_no_smoke.local_path.mkdir()
        phases = [
            PhaseDefinition(
                id="p1",
                title="Logic Feature",
                branch="feat/p8e-e2e",
                prompt="Implement logic",
                allowed_globs=["src/**"],
                auto_merge=True,
                release_after_merge=True,
            )
        ]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"), patch(
            "nightshift.engine.logins.resolve_bin", return_value="tool.exe"
        ), patch(
            "nightshift.engine.state_machine.shutil.disk_usage",
            return_value=(20 * 1024**3, 10 * 1024**3, 10 * 1024**3),
        ):
            rows1 = await engine_no_smoke.execute_plan(project_no_smoke, phases, plan_id="plan_8e")

        assert "feat/p8e-e2e" in vcs.merged_prs
        assert rows1[0]["status"] == "PARKED"
        task_after_1 = store.get_task_by_branch("feat/p8e-e2e")
        assert task_after_1 is not None
        assert task_after_1["status"] == "BLOCKED"
        assert task_after_1["block_reason"] == "smoke_test_unavailable"
        assert task_after_1["blocked_from_status"] == "MERGED"
        assert task_after_1["smoke_verified_at"] is None

        # Deck surfaces the blocked smoke item in needs_you
        deck = generate_deck_data(store, "proj_8e")
        assert any("smoke test" in item["headline"].lower() for item in deck["needs_you"])

        # Phase 2: Resume with smoke_command configured & passing smoke_runner -> transitions MERGED -> RELEASED
        from nightshift.engine.pipeline_orchestrator import TaskOrchestrator

        TaskOrchestrator(store).resume_from_blocked(task_after_1["id"], current_merge_sha="sha_merge_123")
        assert store.get_task(task_after_1["id"])["status"] == "MERGED"

        def pass_smoke(cmd: str, art: Path, tag: str, timeout: int) -> SmokeTestResult:
            return SmokeTestResult(passed=True, status="passed", detail="OK", command=cmd, verified_at="2026-11-28T02:00:00Z")

        engine_with_smoke = EngineStateMachine(
            store,
            events,
            quota,
            vcs=vcs,
            runners=runners,
            login_checker=logins,
            smoke_runner=pass_smoke,
        )
        project_with_smoke = ProjectConfig(
            id="proj_8e",
            name="NightShift 8e",
            repo="owner/repo",
            local_path=project_no_smoke.local_path,
            output_dir=project_no_smoke.output_dir,
            artifact_glob="*.zip",
            smoke_command=DESKTOP_SELFTEST_SMOKE,
        )
        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"), patch(
            "nightshift.engine.logins.resolve_bin", return_value="tool.exe"
        ), patch(
            "nightshift.engine.state_machine.shutil.disk_usage",
            return_value=(20 * 1024**3, 10 * 1024**3, 10 * 1024**3),
        ):
            rows2 = await engine_with_smoke.execute_plan(project_with_smoke, phases, plan_id="plan_8e")

        assert rows2[0]["status"] == "COMPLETED"
        task_after_2 = store.get_task(task_after_1["id"])
        assert task_after_2 is not None
        assert task_after_2["status"] == "RELEASED"
        assert task_after_2["smoke_verified_at"] is not None


def test_bridge_pipeline_approve_task_and_rollback_release(tmp_path: Path) -> None:
    with Store(tmp_path / "bridge_8e.db") as store:
        store.migrate()
        events = EventBus(store)
        vcs = FakeGitGh()
        out_dir = tmp_path / "dist"
        out_dir.mkdir()
        store.create_project("proj-1", "NightShift", "owner/repo", str(tmp_path), output_dir=str(out_dir))
        store.create_plan("plan-1", "proj-1", "Plan")
        store.upsert_phase("p1", "plan-1", "Gov Phase", "feat/gov")
        task, contract = store.create_task_with_contract(
            task_id="t-gov",
            phase_id="p1",
            plan_id="plan-1",
            title="Gov Task",
            branch="feat/gov",
            risk_class="governance",
        )
        store.update_task_fields(
            "t-gov",
            status="BLOCKED",
            blocked_from_status="REVIEWING",
            head_sha="sha_gov_1",
            review_sha="sha_gov_1",
            block_reason="awaiting_owner_approval",
        )
        store.record_evidence(
            task_id="t-gov",
            contract_sha256=contract["contract_sha256"],
            commit_sha="sha_gov_1",
            kind="ci_check",
            source="circleci:desktop-gates",
            build_id="b-1",
            duration_seconds=10.0,
            verified=True,
        )

        # Deck surfaces governance approval in needs_you
        deck = generate_deck_data(store, "proj-1")
        assert any("approval" in item["headline"].lower() for item in deck["needs_you"])

        class _StubEngine:
            def __init__(self, vcs_obj: FakeGitGh) -> None:
                self.vcs = vcs_obj
                self.release_mgr = ReleaseManager(vcs_obj)

        bridge = BridgeAPI(store, events, ModuleRegistry(), engine=_StubEngine(vcs))

        # Mismatched SHA rejected
        bad_approve = bridge.call("pipeline.approveTask", {"taskId": "t-gov", "headSha": "sha_wrong"})
        assert bad_approve["ok"] is False

        # Exact SHA approved and resumed to APPROVED
        ok_approve = bridge.call("pipeline.approveTask", {"taskId": "t-gov", "headSha": "sha_gov_1", "approvedBy": "owner"})
        assert ok_approve["ok"] is True
        assert ok_approve["data"]["status"] == "APPROVED"
        assert ok_approve["data"]["owner_approved_sha"] == "sha_gov_1"

        # Assertion waiver via bridge
        waiver = bridge.call("pipeline.recordAssertionWaiver", {"taskId": "t-gov", "headSha": "sha_gov_1", "reason": "Consolidated duplicate test"})
        assert waiver["ok"] is True
        assert waiver["data"]["assertion_waiver_sha"] == "sha_gov_1"

        # Rollback via bridge
        good_file = tmp_path / "app.zip"
        good_file.write_bytes(b"good-zip-v1")
        good_sums = tmp_path / "SHA256SUMS.txt"
        good_sha = compute_sha256(good_file)
        good_sums.write_text(f"{good_sha}  app.zip\n", encoding="utf-8")
        vcs.create_github_release("owner/repo", "v1.0.0-preview.1", "v1", "Good", [good_file, good_sums])
        vcs.create_github_release("owner/repo", "v1.0.0-preview.2", "v2", "Bad", [good_file, good_sums])
        store.record_release_verification(
            tag="v1.0.0-preview.1",
            repo="owner/repo",
            merge_sha="sha_1",
            artifact_sha256=good_sha,
            smoke_command=DESKTOP_SELFTEST_SMOKE,
            smoke_status="passed",
            verified_at="2026-11-28T01:00:00Z",
        )
        store.record_release_verification(
            tag="v1.0.0-preview.2",
            repo="owner/repo",
            merge_sha="sha_2",
            previous_good_tag="v1.0.0-preview.1",
            artifact_sha256="deadbeef",
            smoke_command=DESKTOP_SELFTEST_SMOKE,
            smoke_status="failed",
        )

        rb_res = bridge.call("pipeline.rollbackRelease", {"tag": "v1.0.0-preview.2", "projectId": "proj-1"})
        assert rb_res["ok"] is True
        assert rb_res["data"]["restored_tag"] == "v1.0.0-preview.1"
        assert (out_dir / "app.zip").read_bytes() == b"good-zip-v1"


# ---------------------------------------------------------------------------
# 6. Regression tests for Codex review findings (P1/P2) & fail-closed hardening
# ---------------------------------------------------------------------------


def test_release_verifications_scoped_by_repo_and_tag(tmp_path: Path) -> None:
    """Two repos publishing the same tag must never overwrite, satisfy, or roll back each other."""
    with Store(tmp_path / "repo_scoped_rel.db") as store:
        store.migrate()
        events = EventBus(store)
        vcs = FakeGitGh()
        out_a = tmp_path / "dist_a"
        out_b = tmp_path / "dist_b"
        out_a.mkdir()
        out_b.mkdir()
        store.create_project("proj-a", "Repo A", "owner/repo-a", str(tmp_path), output_dir=str(out_a))
        store.create_project("proj-b", "Repo B", "owner/repo-b", str(tmp_path), output_dir=str(out_b))

        art_a = tmp_path / "a.zip"
        art_a.write_bytes(b"repo-a-good-v1")
        sha_a = compute_sha256(art_a)
        sums_a = tmp_path / "SHA256SUMS.txt"
        sums_a.write_text(f"{sha_a}  a.zip\n", encoding="utf-8")

        vcs.create_github_release("owner/repo-a", "v1.0.0-preview.1", "A v1", "Good A", [art_a, sums_a])
        vcs.create_github_release("owner/repo-a", "v1.0.0-preview.2", "A v2", "Bad A", [art_a, sums_a])

        # Record passing v1 and failing v2 for repo-a
        store.record_release_verification(
            repo="owner/repo-a",
            tag="v1.0.0-preview.1",
            merge_sha="sha_a1",
            artifact_sha256=sha_a,
            smoke_command=DESKTOP_SELFTEST_SMOKE,
            smoke_status="passed",
            verified_at="2026-11-28T01:00:00Z",
        )
        store.record_release_verification(
            repo="owner/repo-a",
            tag="v1.0.0-preview.2",
            merge_sha="sha_a2",
            previous_good_tag="v1.0.0-preview.1",
            artifact_sha256="deadbeef",
            smoke_command=DESKTOP_SELFTEST_SMOKE,
            smoke_status="failed",
        )

        # Record same tags for repo-b with completely different statuses/SHAs
        store.record_release_verification(
            repo="owner/repo-b",
            tag="v1.0.0-preview.1",
            merge_sha="sha_b1",
            artifact_sha256="1111" * 16,
            smoke_command="smoke-b",
            smoke_status="failed",
            smoke_detail="Repo B failed",
        )
        store.record_release_verification(
            repo="owner/repo-b",
            tag="v1.0.0-preview.2",
            merge_sha="sha_b2",
            artifact_sha256="2222" * 16,
            smoke_command="smoke-b",
            smoke_status="passed",
            verified_at="2026-11-28T02:00:00Z",
        )

        # Verify repo-a and repo-b records remain isolated
        rec_a1 = store.get_release_verification("owner/repo-a", "v1.0.0-preview.1")
        rec_b1 = store.get_release_verification("owner/repo-b", "v1.0.0-preview.1")
        assert rec_a1 is not None and rec_a1["smoke_status"] == "passed" and rec_a1["merge_sha"] == "sha_a1"
        assert rec_b1 is not None and rec_b1["smoke_status"] == "failed" and rec_b1["merge_sha"] == "sha_b1"

        # Deck for proj-a only surfaces owner/repo-a's failed release, not owner/repo-b's
        deck_a = generate_deck_data(store, "proj-a")
        release_items = [item for item in deck_a["needs_you"] if item.get("release_tag")]
        assert all(item.get("repo") == "owner/repo-a" for item in release_items)

        class _StubEngine:
            def __init__(self, vcs_obj: FakeGitGh) -> None:
                self.vcs = vcs_obj
                self.release_mgr = ReleaseManager(vcs_obj)

        bridge = BridgeAPI(store, events, ModuleRegistry(), engine=_StubEngine(vcs))

        # Rollback without repo/projectId fails closed
        no_repo_res = bridge.call("pipeline.rollbackRelease", {"tag": "v1.0.0-preview.2"})
        assert no_repo_res["ok"] is False

        # Rollback for proj-a updates ONLY owner/repo-a's v1.0.0-preview.2, leaving owner/repo-b untouched
        rb_a = bridge.call("pipeline.rollbackRelease", {"tag": "v1.0.0-preview.2", "projectId": "proj-a"})
        assert rb_a["ok"] is True
        rec_a2_after = store.get_release_verification("owner/repo-a", "v1.0.0-preview.2")
        rec_b2_after = store.get_release_verification("owner/repo-b", "v1.0.0-preview.2")
        assert rec_a2_after is not None and rec_a2_after["smoke_status"] == "rolled_back"
        assert rec_b2_after is not None and rec_b2_after["smoke_status"] == "passed"


def test_ui_files_escalate_to_business_logic_unless_diff_verified_cosmetic(tmp_path: Path) -> None:
    """Executable logic or unverifiable diffs in UI directories must escalate to business_logic."""
    # 1. Event handler / bridge call in Page.tsx -> escalates to business_logic
    exec_diff = (
        "diff --git a/frontend/src/modules/mission-control/Page.tsx b/frontend/src/modules/mission-control/Page.tsx\n"
        "@@ -10,3 +10,4 @@\n"
        " const Page = () => {\n"
        '+  const res = await bridge.call("engine.start", {});\n'
        '+  return <Button onClick={handleStart}>Start</Button>;\n'
    )
    res_exec = classify_task_risk(
        [DiffPathEntry("M", "frontend/src/modules/mission-control/Page.tsx")],
        exec_diff,
        base_risk_class="ui_cosmetic",
    )
    assert res_exec.risk_class == "business_logic"
    assert res_exec.requires_independent_review is True

    # 2. Empty diff content on UI file -> fail closed to business_logic
    res_empty = classify_task_risk(
        [DiffPathEntry("M", "frontend/src/modules/mission-control/Page.tsx")],
        "",
        base_risk_class="ui_cosmetic",
    )
    assert res_empty.risk_class == "business_logic"
    assert res_empty.requires_independent_review is True

    # 3. Non-JSX .ts file under frontend/src/modules -> escalates to business_logic
    res_ts = classify_task_risk(
        [DiffPathEntry("M", "frontend/src/modules/mission-control/api.ts")],
        "+export const POLL_MS = 500;\n",
        base_risk_class="ui_cosmetic",
    )
    assert res_ts.risk_class == "business_logic"

    # 4. Structural deletion of UI component -> escalates to business_logic
    res_del = classify_task_risk(
        [DiffPathEntry("D", "frontend/src/modules/mission-control/Page.tsx")],
        "-<GlassCard />\n",
        base_risk_class="ui_cosmetic",
    )
    assert res_del.risk_class == "business_logic"

    # 5. Verified cosmetic styling/copy diff -> remains ui_cosmetic
    cosmetic_diff = (
        "diff --git a/frontend/src/modules/mission-control/Page.tsx b/frontend/src/modules/mission-control/Page.tsx\n"
        "@@ -1,3 +1,3 @@\n"
        '-<GlassCard className="p-4" style={{ padding: "var(--space-4)" }}>\n'
        '+<GlassCard className="p-6" style={{ padding: "var(--space-6)" }}>\n'
        "-  Old title\n"
        "+  New title\n"
    )
    res_cosmetic = classify_task_risk(
        [DiffPathEntry("M", "frontend/src/modules/mission-control/Page.tsx")],
        cosmetic_diff,
        base_risk_class="docs_format",
    )
    assert res_cosmetic.risk_class == "ui_cosmetic"
    assert res_cosmetic.requires_independent_review is False

    # 6. MergeGate blocks executable Page.tsx edit without independent review even with verified screenshots
    with Store(tmp_path / "ui_gate.db") as store:
        store.migrate()
        t_ui, c_ui = _seed_task(
            store,
            task_id="t-ui-logic",
            branch="feat/ui-logic",
            risk_class="ui_cosmetic",
            allowed_globs=["frontend/**"],
        )
        store.record_evidence(
            task_id="t-ui-logic",
            contract_sha256=c_ui["contract_sha256"],
            commit_sha="sha_uilogic",
            kind="ci_check",
            source="circleci:desktop-frontend",
            build_id="b-1",
            duration_seconds=30.0,
            verified=True,
        )
        store.record_evidence(
            task_id="t-ui-logic",
            contract_sha256=c_ui["contract_sha256"],
            commit_sha="sha_uilogic",
            kind="screenshot",
            source="playwright:manifest",
            build_id="b-1",
            metadata={"renderer": "playwright-mockbridge"},
            verified=True,
        )
        gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=True)
        auth = gate.authorize_merge(
            task=t_ui,
            contract=c_ui,
            head_sha="sha_uilogic",
            diff_entries=[DiffPathEntry("M", "frontend/src/modules/mission-control/Page.tsx")],
            diff_content=exec_diff,
            evidence_records=store.list_evidence_records("t-ui-logic", commit_sha="sha_uilogic"),
        )
        assert auth.allowed is False
        assert auth.effective_risk_class == "business_logic"
        assert auth.block_reason == "no_independent_reviewer"


@pytest.mark.asyncio
async def test_review_rejects_omitted_or_null_findings_array(tmp_path: Path) -> None:
    """APPROVE without an explicit findings array (or with findings: null) is malformed and never approves."""
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    contract = {"acceptance_criteria": [{"id": "ac-1", "description": "Works"}]}

    # 1. Missing findings key
    res_missing = parse_and_validate_review_output(
        '{"verdict": "APPROVE", "summary": "LGTM"}',
        worktree_dir=wt,
        contract=contract,
    )
    assert res_missing.valid is False
    assert res_missing.verdict == "INVALID"
    assert any("findings" in e for e in res_missing.errors)

    # 2. Explicit findings: null
    res_null = parse_and_validate_review_output(
        '{"verdict": "APPROVE", "summary": "LGTM", "findings": null}',
        worktree_dir=wt,
        contract=contract,
    )
    assert res_null.valid is False
    assert res_null.verdict == "INVALID"
    assert any("findings" in e for e in res_null.errors)

    # 3. End-to-end in CiPipelineManager: missing findings triggers retry and blocks if still omitted
    with Store(tmp_path / "missing_findings.db") as store:
        store.migrate()
        _, db_contract = _seed_task(store, task_id="t-mf", branch="feat/mf")
        vcs = FakeGitGh()
        vcs.add_pr("feat/mf", number=303, head_sha="sha_303")
        vcs.worktrees["feat/mf"] = wt
        vcs.changed_files["feat/mf"] = ["src/main.py"]

        codex_bad = FakeRunner(
            name="codex",
            responses=[
                RunnerResult(rc=0, stdout='{"verdict": "APPROVE", "summary": "LGTM"}', stderr=""),
                RunnerResult(rc=0, stdout='{"verdict": "APPROVE", "findings": null}', stderr=""),
            ],
        )
        ci_mgr = CiPipelineManager(
            vcs,
            store,
            Scheduler(store, QuotaLedger(store)),
            {"agy": FakeRunner.success("agy"), "codex": codex_bad},
        )
        ci_res = await ci_mgr.watch_and_heal(
            repo="owner/repo",
            local_repo=tmp_path,
            worktree_dir=wt,
            phase_id="p1",
            phase_title="P1",
            branch="feat/mf",
            pr_number=303,
            initial_engine="agy",
            risk=RiskAssessment(level="low"),
            task_contract=db_contract,
            contributing_providers={"google"},
        )
        assert ci_res.passed is False
        assert ci_res.block_reason == "invalid_review_output"
        assert len(codex_bad.calls) == 2


def test_merge_gate_review_evidence_tier_and_unverified_rollback_fail_closed(tmp_path: Path) -> None:
    """MergeGate rejects economy review evidence for high_risk; rollback refuses unverified target."""
    with Store(tmp_path / "hardening.db") as store:
        store.migrate()
        t_hr, c_hr = _seed_task(
            store,
            task_id="t-hr-ev",
            branch="feat/hr-ev",
            risk_class="high_risk",
            allowed_globs=["backend/**"],
        )
        store.record_task_contributing_provider("t-hr-ev", "google")
        store.record_task_owner_approval("t-hr-ev", "owner", "sha_hr_ev")
        store.record_evidence(
            task_id="t-hr-ev",
            contract_sha256=c_hr["contract_sha256"],
            commit_sha="sha_hr_ev",
            kind="ci_check",
            source="circleci:desktop-backend",
            build_id="b-1",
            duration_seconds=20.0,
            verified=True,
        )
        # Persist an economy-tier review evidence record for a high_risk task
        store.record_evidence(
            task_id="t-hr-ev",
            contract_sha256=c_hr["contract_sha256"],
            commit_sha="sha_hr_ev",
            kind="review",
            source="review:qwen",
            metadata={
                "reviewer_engine": "qwen",
                "reviewer_provider": "alibaba",
                "reviewer_tier": "economy",
                "verdict": "APPROVE",
                "p1_count": 0,
            },
            verified=True,
        )
        gate = MergeGate(autonomous_merge_enabled=True, require_independent_reviewer=True)
        auth = gate.authorize_merge(
            task=store.get_task("t-hr-ev") or t_hr,
            contract=c_hr,
            head_sha="sha_hr_ev",
            diff_entries=[DiffPathEntry("M", "backend/nightshift/engine/credentials.py")],
            evidence_records=store.list_evidence_records("t-hr-ev", commit_sha="sha_hr_ev"),
            require_review_evidence=True,
            reviewer_provider="openai",
            reviewer_engine="codex",
            reviewer_tier="standard",
        )
        assert auth.allowed is False
        assert auth.block_reason == "missing_verified_review"

        # Rollback refuses if previous_good_tag is not a verified passed release
        vcs = FakeGitGh()
        store.record_release_verification(
            repo="owner/repo",
            tag="v1.0.0-preview.1",
            merge_sha="sha_1",
            artifact_sha256="a" * 64,
            smoke_command=DESKTOP_SELFTEST_SMOKE,
            smoke_status="failed",
        )
        store.record_release_verification(
            repo="owner/repo",
            tag="v1.0.0-preview.2",
            merge_sha="sha_2",
            previous_good_tag="v1.0.0-preview.1",
            artifact_sha256="b" * 64,
            smoke_command=DESKTOP_SELFTEST_SMOKE,
            smoke_status="failed",
        )
        rb = ReleaseManager(vcs).rollback_release(
            store=store,
            repo="owner/repo",
            faulty_tag="v1.0.0-preview.2",
            output_dir=tmp_path / "dist",
        )
        assert rb.ok is False
        assert "not a verified passed release" in rb.detail
