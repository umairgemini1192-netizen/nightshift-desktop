"""Phase 8b tests: Trust boundaries, frozen contracts, protected test authority, scrubbed env, and MergeGate.

Covers docs/AGENT_PIPELINE.md section 12.1 Phase 8b requirements (Amendments A, B, E).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.pipeline_contracts import (
    DiffPathEntry,
    MergeGate,
    canonical_contract_json,
    check_contract_path_guards,
    compute_contract_sha256,
    detect_assertion_relaxation_for_files,
    detect_js_assertion_relaxation,
    detect_python_assertion_relaxation,
    is_governance_path,
    parse_name_status_diff,
    provider_for_engine,
    verify_contract_integrity,
)
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
from nightshift.engine.vcs import FakeGitGh
from nightshift.engine.verify import check_diff_guards
from nightshift.events import EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners import AgyRunner, ClaudeRunner, CodexRunner, FakeRunner, GeminiRunner, TemplateRunner
from nightshift.runners.base import build_scrubbed_env, run_subprocess_with_timeout
from nightshift.store import Store


def _ok_login_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    return (0, '{"loggedIn": true}\nLogged in to github.com\nOK', "", False)


# ---------------------------------------------------------------------------
# 1. Frozen Requirement Contracts (Amendment B, AGENT_PIPELINE §3.1, §5)
# ---------------------------------------------------------------------------


def test_frozen_contract_canonical_json_excludes_digest_and_triggers_block_mutation(tmp_path: Path) -> None:
    with Store(tmp_path / "contracts.db") as store:
        store.migrate()
        store.create_project("proj", "Proj", "o/r", str(tmp_path))
        store.create_plan("plan-1", "proj", "Plan")
        store.upsert_phase("p1", "plan-1", "Phase 1", "feat/p1")

        task, contract = store.create_task_with_contract(
            task_id="p8b-t01",
            phase_id="p1",
            plan_id="plan-1",
            title="Enforce allowed_globs",
            branch="feat/p1",
            risk_class="business_logic",
            allowed_globs=["backend/nightshift/engine/verify.py"],
        )

        # Canonical JSON must exclude contract_sha256 itself
        parsed = json.loads(contract["canonical_json"])
        assert "contract_sha256" not in parsed
        assert compute_contract_sha256(contract["canonical_json"]) == contract["contract_sha256"]
        assert task["contract_sha256"] == contract["contract_sha256"]

        ok, msg = verify_contract_integrity(contract, expected_sha256=task["contract_sha256"])
        assert ok is True, msg

        # Tampered digest must fail verification
        bad_contract = dict(contract)
        bad_contract["contract_sha256"] = "0" * 64
        ok_bad, _ = verify_contract_integrity(bad_contract, expected_sha256=task["contract_sha256"])
        assert ok_bad is False

        # SQLite BEFORE UPDATE trigger blocks mutation
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute(
                "UPDATE task_contracts SET allowed_globs_json = '[\"*\"]' WHERE id = ?",
                (contract["id"],),
            )

        # SQLite BEFORE DELETE trigger blocks deletion
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.connection.execute(
                "DELETE FROM task_contracts WHERE id = ?",
                (contract["id"],),
            )


def test_contract_version_bump_invalidates_prior_evidence_review_and_approvals(tmp_path: Path) -> None:
    with Store(tmp_path / "revise.db") as store:
        store.migrate()
        store.create_project("proj", "Proj", "o/r", str(tmp_path))
        store.create_plan("plan-1", "proj", "Plan")
        store.upsert_phase("p1", "plan-1", "Phase 1", "feat/p1")

        task, v1 = store.create_task_with_contract(
            task_id="p8b-t02",
            phase_id="p1",
            plan_id="plan-1",
            title="Revise contract test",
            branch="feat/p1",
            allowed_globs=["backend/nightshift/engine/verify.py"],
        )

        # Record prior evidence, review, owner approval, and assertion waiver against v1
        store.record_evidence(
            task_id=task["id"],
            contract_sha256=v1["contract_sha256"],
            commit_sha="sha_aaa",
            kind="ci_check",
            source="circleci:desktop-backend",
            duration_seconds=45.0,
            verified=True,
        )
        store.update_task_fields(task["id"], review_sha="sha_aaa", approved_sha="sha_aaa")
        store.record_task_owner_approval(task["id"], "owner", "sha_aaa")
        store.record_task_assertion_waiver(task["id"], "sha_aaa", "Legitimate refactor")

        # Revise to v2
        updated_task, v2 = store.revise_task_contract(
            task_id=task["id"],
            allowed_globs=[
                "backend/nightshift/engine/verify.py",
                "backend/nightshift/engine/state_machine.py",
            ],
        )

        assert v2["version"] == 2
        assert v2["contract_sha256"] != v1["contract_sha256"]
        assert updated_task["contract_id"] == v2["id"]
        assert updated_task["contract_sha256"] == v2["contract_sha256"]
        assert updated_task["review_sha"] is None
        assert updated_task["approved_sha"] is None
        assert updated_task["owner_approved_sha"] is None
        assert updated_task["assertion_waiver_sha"] is None
        assert updated_task["assertion_waiver_reason"] == ""

        # Prior evidence must be invalidated
        ev_rows = store.list_evidence_for_task(task["id"])
        assert len(ev_rows) == 1
        assert ev_rows[0]["verified"] == 0
        assert ev_rows[0]["rejection_reason"] == "contract_revised"


# ---------------------------------------------------------------------------
# 2. Rename/Delete/Case-Safe allowed_globs & Governance Classification (Amendment A & B)
# ---------------------------------------------------------------------------


def test_parse_name_status_diff_and_rename_aware_guards() -> None:
    raw = "\n".join([
        "M\tbackend/nightshift/engine/verify.py",
        "R100\tbackend/tests/conftest.py\tbackend/nightshift/helper.py",
        "C090\tscripts/check_tokens.py\tbackend/nightshift/copied.py",
        "D\tfrontend/tests/app.spec.js",
    ])
    entries = parse_name_status_diff(raw)
    assert len(entries) == 4
    assert entries[1] == DiffPathEntry(status="R", old_path="backend/tests/conftest.py", path="backend/nightshift/helper.py")
    assert entries[1].all_paths() == ["backend/tests/conftest.py", "backend/nightshift/helper.py"]
    assert entries[2].all_paths() == ["scripts/check_tokens.py", "backend/nightshift/copied.py"]

    # Renaming a test/governance file into a feature directory must STILL classify as governance
    # and trigger outside_allowed_globs + governance_split_required
    findings, touches_gov = check_contract_path_guards(
        [entries[1]],
        allowed_globs=["backend/nightshift/**"],
        case_fold=True,
    )
    assert touches_gov is True
    assert any(f.guard == "outside_allowed_globs" and f.path == "backend/tests/conftest.py" for f in findings)
    assert any(f.guard == "governance_split_required" for f in findings)


@pytest.mark.parametrize(
    "path,diff_content,expected",
    [
        ("backend/tests/conftest.py", "", True),
        ("CONFTEST.PY", "", True),
        ("frontend/src/foo.spec.js", "", True),
        ("frontend/src/Button.test.jsx", "", True),
        (".circleci/config.yml", "", True),
        (".CircleCI/Config.YML", "", True),
        ("design/tokens.json", "", True),
        ("docs/DESIGN_SYSTEM.md", "", True),
        ("Scripts/Check_UI_Rules.py", "", True),
        ("AGENTS.md", "", True),
        ("backend/pyproject.toml", "+[tool.pytest.ini_options]\n+addopts = '-q'", True),
        ("backend/pyproject.toml", "+version = '0.2.0'", False),
        ("backend/nightshift/engine/verify.py", "", False),
    ],
)
def test_governance_path_detection_with_windows_case_folding(
    path: str, diff_content: str, expected: bool
) -> None:
    assert is_governance_path(path, diff_content=diff_content, case_fold=True) is expected


def test_mixed_feature_and_governance_pr_blocked(tmp_path: Path) -> None:
    entries = [
        DiffPathEntry(status="M", path="backend/nightshift/engine/verify.py"),
        DiffPathEntry(status="M", path="backend/tests/test_verify_guards.py"),
    ]
    findings = check_diff_guards(
        tmp_path,
        ["backend/nightshift/engine/verify.py", "backend/tests/test_verify_guards.py"],
        "",
        diff_entries=entries,
        allowed_globs=["backend/**"],
        case_fold=True,
    )
    assert any(f.guard == "governance_split_required" for f in findings)


# ---------------------------------------------------------------------------
# 3. Blocking Assertion-Relaxation Detector (Amendment A, AGENT_PIPELINE §3.2 rule 4)
# ---------------------------------------------------------------------------


def test_python_assertion_relaxation_detector_catches_all_patterns() -> None:
    before = """
import pytest

def test_alpha():
    assert 1 == 1
    with pytest.raises(ValueError):
        raise ValueError("x")

def test_beta():
    assert True
"""
    # 1. Deleted test file
    f_del = detect_python_assertion_relaxation("backend/tests/test_a.py", before, None)
    assert any(f.guard == "assertion_relaxation" and "Deleted test file" in f.message for f in f_del)

    # 2. Deleted test function
    after_deleted_fn = """
import pytest

def test_alpha():
    assert 1 == 1
    with pytest.raises(ValueError):
        raise ValueError("x")
"""
    f_fn = detect_python_assertion_relaxation("backend/tests/test_a.py", before, after_deleted_fn)
    assert any("Deleted test function 'test_beta'" in f.message for f in f_fn)

    # 3. Net decrease in assertions
    after_fewer = """
import pytest

def test_alpha():
    assert 1 == 1

def test_beta():
    assert True
"""
    f_dec = detect_python_assertion_relaxation("backend/tests/test_a.py", before, after_fewer)
    assert any("Net decrease in assertions" in f.message for f in f_dec)

    # 4. Broad except Exception: pass
    after_broad = before + "\ndef test_gamma():\n    try:\n        assert 1 == 2\n    except Exception:\n        pass\n"
    f_broad = detect_python_assertion_relaxation("backend/tests/test_a.py", before, after_broad)
    assert any("except Exception: pass" in f.message for f in f_broad)

    # 5. New @pytest.mark.skip / @pytest.mark.xfail
    after_skip = before.replace("def test_beta():", "@pytest.mark.skip(reason='flaky')\ndef test_beta():")
    f_skip = detect_python_assertion_relaxation("backend/tests/test_a.py", before, after_skip)
    assert any("@pytest.mark.skip" in f.message for f in f_skip)

    # 6. Strengthening tests (adding assertions) passes cleanly
    after_stronger = before + "\ndef test_gamma():\n    assert 2 + 2 == 4\n"
    assert detect_python_assertion_relaxation("backend/tests/test_a.py", before, after_stronger) == []


def test_js_assertion_relaxation_detector_catches_all_patterns() -> None:
    before = """
import { expect, test } from 'vitest';
import AxeBuilder from '@axe-core/playwright';

test('renders card', async () => {
  expect(title).toHaveText('Mission Control');
  expect(count).toBe(3);
  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations).toEqual([]);
});
"""
    # 1. Deleted test file
    assert detect_js_assertion_relaxation("frontend/tests/card.spec.ts", before, None)

    # 2. Fewer expect() calls
    fewer = before.replace("  expect(count).toBe(3);\n", "")
    assert any("Net decrease in expect()" in f.message for f in detect_js_assertion_relaxation("frontend/tests/card.spec.ts", before, fewer))

    # 3. Strict -> vacuous matcher replacement
    vacuous = before.replace(".toBe(3)", ".toBeTruthy()")
    assert any("vacuous matcher" in f.message for f in detect_js_assertion_relaxation("frontend/tests/card.spec.ts", before, vacuous))

    # 4. Introduced .skip / .only / test.fixme
    skipped = before.replace("test('renders card'", "test.fixme('renders card'")
    assert any(".skip/.only/test.fixme" in f.message for f in detect_js_assertion_relaxation("frontend/tests/card.spec.ts", before, skipped))

    # 5. Removed axe check
    no_axe = before.replace("import AxeBuilder from '@axe-core/playwright';\n", "").replace(
        "  const axe = await new AxeBuilder({ page }).analyze();\n  expect(axe.violations).toEqual([]);\n",
        "  expect(1).toBe(1);\n",
    )
    assert any("Removed axe" in f.message for f in detect_js_assertion_relaxation("frontend/tests/card.spec.ts", before, no_axe))


# ---------------------------------------------------------------------------
# 4. Scrubbed Runner Environment & Output Cap (Amendment E, AGENT_PIPELINE §8.1)
# ---------------------------------------------------------------------------


def test_scrubbed_runner_env_strips_host_secrets_and_isolates_extra_env() -> None:
    host_env = {
        "PATH": "/usr/bin",
        "TEMP": "/tmp",
        "USERPROFILE": "C:\\Users\\owner",
        "AWS_SECRET_ACCESS_KEY": "super-secret-aws",
        "OPENAI_API_KEY": "sk-live-secret",
        "GITHUB_TOKEN": "ghp_host_secret_token",
        "GH_TOKEN": "ghp_host_gh_token",
        "ANTHROPIC_API_KEY": "sk-ant-host",
        "UNRELATED_CUSTOM_VAR": "should-not-leak",
    }
    scrubbed = build_scrubbed_env(
        extra_env={"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
        base_env=host_env,
    )
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["DO_NOT_TRACK"] == "1"
    assert scrubbed["NO_COLOR"] == "1"
    assert scrubbed["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    for forbidden in (
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "UNRELATED_CUSTOM_VAR",
    ):
        assert forbidden not in scrubbed


def test_run_subprocess_with_scrub_env_strips_host_secret_in_real_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaked_aws_key_123")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_leaked_token_123")
    code = (
        "import os, json; "
        "print(json.dumps({"
        "'aws': os.environ.get('AWS_SECRET_ACCESS_KEY'), "
        "'gh': os.environ.get('GITHUB_TOKEN'), "
        "'dnt': os.environ.get('DO_NOT_TRACK'), "
        "'extra': os.environ.get('NS_ONLY_THIS_PROC')"
        "}))"
    )
    rc, out, err, timed_out = run_subprocess_with_timeout(
        [sys.executable, "-c", code],
        env={"NS_ONLY_THIS_PROC": "yes"},
        scrub_env=True,
        timeout_seconds=15,
    )
    assert rc == 0, err
    assert timed_out is False
    data = json.loads(out)
    assert data["aws"] is None
    assert data["gh"] is None
    assert data["dnt"] == "1"
    assert data["extra"] == "yes"


@pytest.mark.asyncio
async def test_all_ai_runners_invoke_subprocess_with_scrub_env_true(tmp_path: Path) -> None:
    runners = [
        ("nightshift.runners.agy.run_subprocess_with_timeout", AgyRunner()),
        ("nightshift.runners.claude.run_subprocess_with_timeout", ClaudeRunner()),
        ("nightshift.runners.codex.run_subprocess_with_timeout", CodexRunner()),
        ("nightshift.runners.gemini.run_subprocess_with_timeout", GeminiRunner()),
        ("nightshift.runners.template.run_subprocess_with_timeout", TemplateRunner("qwen", ["qwen", "-p", "{instruction}"])),
    ]
    for target, runner in runners:
        with patch(target, return_value=(0, "ok", "", False)) as mock_run:
            await runner.run("hello", tmp_path)
            assert mock_run.called
            assert mock_run.call_args.kwargs.get("scrub_env") is True, f"{runner.name} did not pass scrub_env=True"


# ---------------------------------------------------------------------------
# 5. MergeGate Chokepoint & EngineStateMachine Integration (Amendments A, B, E)
# ---------------------------------------------------------------------------


def test_merge_gate_blocks_assertion_relaxation_unless_exact_sha_governance_waiver(tmp_path: Path) -> None:
    with Store(tmp_path / "gate.db") as store:
        store.migrate()
        store.create_project("proj", "Proj", "o/r", str(tmp_path))
        store.create_plan("plan-1", "proj", "Plan")
        store.upsert_phase("p1", "plan-1", "Gov PR", "feat/gov-1")
        task, contract = store.create_task_with_contract(
            task_id="t-gov-1",
            phase_id="p1",
            plan_id="plan-1",
            title="Refactor tests",
            branch="feat/gov-1",
            risk_class="governance",
            allowed_globs=["backend/tests/**"],
        )
        store.record_task_contributing_provider("t-gov-1", "google")
        store.record_task_owner_approval("t-gov-1", "owner", "sha_head_1")
        task = store.get_task("t-gov-1")
        assert task is not None

        gate = MergeGate(autonomous_merge_enabled=False)
        snapshots = {
            "backend/tests/test_sample.py": (
                "def test_one():\n    assert 1 == 1\n    assert 2 == 2\n",
                "def test_one():\n    assert 1 == 1\n",
            )
        }
        entries = [DiffPathEntry(status="M", path="backend/tests/test_sample.py")]

        # Blocked without waiver even on a dedicated governance PR with owner approval!
        res_no_waiver = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_head_1",
            diff_entries=entries,
            test_file_snapshots=snapshots,
            reviewer_provider="openai",
        )
        assert res_no_waiver.allowed is False
        assert res_no_waiver.block_reason == "assertion_relaxation"

        # Blocked with waiver for WRONG SHA
        store.record_task_assertion_waiver("t-gov-1", "sha_other", "Valid refactor")
        task = store.get_task("t-gov-1")
        assert task is not None
        res_wrong_sha = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_head_1",
            diff_entries=entries,
            test_file_snapshots=snapshots,
            reviewer_provider="openai",
        )
        assert res_wrong_sha.allowed is False
        assert res_wrong_sha.block_reason == "assertion_relaxation"

        # Allowed with exact-SHA waiver + rationale + owner approval + independent reviewer
        store.record_task_assertion_waiver("t-gov-1", "sha_head_1", "Consolidated duplicate assert")
        task = store.get_task("t-gov-1")
        assert task is not None
        res_ok = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_head_1",
            diff_entries=entries,
            test_file_snapshots=snapshots,
            reviewer_provider="openai",
        )
        assert res_ok.allowed is True, res_ok.reason


def test_merge_gate_blocks_same_provider_review_including_agy_and_gemini(tmp_path: Path) -> None:
    assert provider_for_engine("agy") == "google"
    assert provider_for_engine("gemini") == "google"
    assert provider_for_engine("codex") == "openai"

    with Store(tmp_path / "indep.db") as store:
        store.migrate()
        store.create_project("proj", "Proj", "o/r", str(tmp_path))
        store.create_plan("plan-1", "proj", "Plan")
        store.upsert_phase("p1", "plan-1", "Logic PR", "feat/logic")
        task, contract = store.create_task_with_contract(
            task_id="t-logic",
            phase_id="p1",
            plan_id="plan-1",
            title="Logic change",
            branch="feat/logic",
            risk_class="business_logic",
            allowed_globs=["src/**"],
        )
        store.record_task_contributing_provider("t-logic", provider_for_engine("agy"))
        store.record_task_owner_approval("t-logic", "owner", "sha_1")
        task = store.get_task("t-logic")
        assert task is not None

        gate = MergeGate(autonomous_merge_enabled=False)
        res = gate.authorize_merge(
            task=task,
            contract=contract,
            head_sha="sha_1",
            diff_entries=[DiffPathEntry(status="M", path="src/app.py")],
            reviewer_provider=provider_for_engine("gemini"),
        )
        assert res.allowed is False
        assert res.block_reason == "reviewer_not_independent"


@pytest.mark.asyncio
async def test_state_machine_creates_task_contract_and_blocks_autonomous_merge_by_default(tmp_path: Path) -> None:
    """Integration test: EngineStateMachine creates a pipeline task + frozen contract automatically,
    routes through MergeGate with verified reviewer_provider, blocks autonomous merge by default
    (staged rollout safety rule), and merges ONLY after exact-SHA owner approval.
    """
    with Store(tmp_path / "sm_p8b.db") as store:
        store.migrate()
        events = EventBus(store)
        quota = QuotaLedger(store)
        vcs = FakeGitGh()

        def on_call(prompt: str, wd: Path, model: str | None, effort: str) -> None:
            vcs.add_pr("feat/p8b-demo", number=777, head_sha="sha_verified_777")
            vcs.changed_files["feat/p8b-demo"] = ["src/feature.py"]

        from nightshift.runners.base import RunnerResult
        runners = {
            "agy": FakeRunner(name="agy", on_call=on_call),
            "codex": FakeRunner(
                name="codex",
                responses=[RunnerResult(rc=0, stdout='{"verdict":"APPROVE","summary":"OK","findings":[]}', stderr="")],
            ),
        }
        logins = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner)
        # Default MergeGate has autonomous_merge_enabled=False!
        engine = EngineStateMachine(store, events, quota, vcs=vcs, runners=runners, login_checker=logins)
        assert engine.merge_gate.autonomous_merge_enabled is False

        project = ProjectConfig(
            id="proj_p8b",
            name="P8b Test",
            repo="o/r",
            local_path=tmp_path / "repo",
            output_dir=tmp_path / "out",
        )
        project.local_path.mkdir()
        phases = [
            PhaseDefinition(
                id="p1",
                title="Feature Task",
                branch="feat/p8b-demo",
                prompt="Implement feature",
                allowed_globs=["src/**"],
                auto_merge=True,
            )
        ]

        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"), patch(
            "nightshift.engine.logins.resolve_bin", return_value="tool.exe"
        ), patch(
            "nightshift.engine.state_machine.shutil.disk_usage",
            return_value=(20 * 1024**3, 10 * 1024**3, 10 * 1024**3),
        ):
            rows_first = await engine.execute_plan(project, phases, plan_id="plan_p8b")

        # 1. Pipeline task + frozen contract were created automatically in production path
        task = store.get_task_by_branch("feat/p8b-demo")
        assert task is not None
        assert task["status"] == "BLOCKED"
        assert task["block_reason"] == "awaiting_owner_approval"
        assert task["review_sha"] == "sha_verified_777"
        assert json.loads(task["contributing_providers_json"]) == ["google"]
        assert rows_first[0]["status"] == "PARKED"
        assert "feat/p8b-demo" not in vcs.merged_prs

        # 2. Owner approves exact head SHA -> resume passes MergeGate (with real reviewer_provider="openai") and merges!
        store.record_task_owner_approval(task["id"], "owner", "sha_verified_777")
        with patch("nightshift.engine.state_machine.WORKTREE_ROOT", tmp_path / "wt"), patch(
            "nightshift.engine.logins.resolve_bin", return_value="tool.exe"
        ), patch(
            "nightshift.engine.state_machine.shutil.disk_usage",
            return_value=(20 * 1024**3, 10 * 1024**3, 10 * 1024**3),
        ):
            rows_second = await engine.execute_plan(project, phases, plan_id="plan_p8b")

        assert rows_second[0]["status"] == "COMPLETED"
        assert "feat/p8b-demo" in vcs.merged_prs
        merged_task = store.get_task(task["id"])
        assert merged_task is not None
        assert merged_task["status"] == "MERGED"
        assert merged_task["approved_sha"] == "sha_verified_777"


def test_parse_name_status_diff_nul_delimited_preserves_literal_filenames() -> None:
    raw = (
        "A\0tests/test_é.py\0"
        "M\0 tests/leading space.py\0"
        "R100\0tests/old_é.py\0tests/new name\twith_tab.py\0"
        "C090\0scripts/check_tokens.py\0src/copied é.py\0"
    )
    entries = parse_name_status_diff(raw, nul_delimited=True)
    assert entries == [
        DiffPathEntry(status="A", path="tests/test_é.py"),
        DiffPathEntry(status="M", path=" tests/leading space.py"),
        DiffPathEntry(status="R", old_path="tests/old_é.py", path="tests/new name\twith_tab.py"),
        DiffPathEntry(status="C", old_path="scripts/check_tokens.py", path="src/copied é.py"),
    ]
    assert parse_name_status_diff("", nul_delimited=True) == []


def test_subprocess_vcs_preserves_unicode_filenames_for_governance_and_snapshots(tmp_path: Path) -> None:
    import subprocess
    from nightshift.engine.vcs import SubprocessGitGh

    repo = tmp_path / "unicode_repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        res = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert res.returncode == 0

    _git("init")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test User")
    # Explicitly ensure git's default path quoting is enabled.
    _git("config", "core.quotepath", "true")

    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git("add", "README.md")
    _git("commit", "-m", "initial")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    unicode_test = tests_dir / "test_é.py"
    original_src = "def test_unicode():\n    assert 1 == 1\n    assert 2 == 2\n"
    unicode_test.write_text(original_src, encoding="utf-8")
    _git("add", "--all")
    _git("commit", "-m", "add unicode test")

    vcs = SubprocessGitGh()
    entries = vcs.list_changed_entries(repo, base="HEAD~1")
    assert entries == [DiffPathEntry(status="A", path="tests/test_é.py")]
    assert vcs.list_changed_files(repo, base="HEAD~1") == ["tests/test_é.py"]
    assert is_governance_path(entries[0].path) is True

    added_snaps = vcs.get_test_file_snapshots(repo, entries, base="HEAD~1")
    assert "tests/test_é.py" in added_snaps
    assert added_snaps["tests/test_é.py"] == (None, original_src)

    # Relax assertion in the Unicode test file and verify snapshot + relaxation detector catches it.
    relaxed_src = "def test_unicode():\n    assert 1 == 1\n"
    unicode_test.write_text(relaxed_src, encoding="utf-8")
    mod_entries = vcs.list_changed_entries(repo, base="HEAD")
    assert mod_entries == [DiffPathEntry(status="M", path="tests/test_é.py")]
    mod_snaps = vcs.get_test_file_snapshots(repo, mod_entries, base="HEAD")
    assert "tests/test_é.py" in mod_snaps
    assert mod_snaps["tests/test_é.py"][0] is not None
    assert "assert 2 == 2" in mod_snaps["tests/test_é.py"][0]
    assert mod_snaps["tests/test_é.py"][1] == relaxed_src

    findings = detect_assertion_relaxation_for_files(mod_snaps)
    assert any(f.guard == "assertion_relaxation" for f in findings)

