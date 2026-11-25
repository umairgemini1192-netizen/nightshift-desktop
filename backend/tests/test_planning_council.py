"""Tests for AI Planning Council (BLUEPRINT 5b).

Tests:
  - Happy path council flow (Gemini draft -> GPT critique -> Claude final)
  - Strict JSON validation with 1 retry on invalid JSON
  - Claude skipped automatically when budget pressure is high or low quota
  - Role substitution through Router when assigned AI is unavailable
  - Council runs and token estimates persisted to council_runs
  - Repo snapshot fallback to built-in compact tree + summaries
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nightshift.engine.council import (
    CouncilRoleResult,
    PlanningCouncil,
    extract_json_payload,
    generate_repo_snapshot,
)
from nightshift.engine.router import Router, seed_default_engines
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.fake import FakeRunner
from nightshift.store import Store


@pytest.fixture
def council_store(tmp_path: Path) -> Store:
    db_path = tmp_path / "test_council.db"
    store = Store(db_path)
    store.migrate()
    seed_default_engines(store)
    return store


@pytest.fixture
def council_ledger(council_store: Store) -> QuotaLedger:
    return QuotaLedger(council_store)


@pytest.fixture
def council_router(council_store: Store, council_ledger: QuotaLedger) -> Router:
    return Router(council_store, council_ledger)


@pytest.mark.asyncio
async def test_council_happy_path_flow(
    council_store: Store,
    council_router: Router,
    tmp_path: Path,
) -> None:
    """Verify happy path 3-role Planning Council deliberation and persistence."""
    draft_json = {
        "title": "Offline Caching Plan",
        "phases": [
            {"id": "p1", "title": "Cache DB", "branch": "feat/p1-cache", "prompt": "Create DB table", "depends_on": []},
            {"id": "p2", "title": "Sync Worker", "branch": "feat/p2-sync", "prompt": "Run sync worker", "depends_on": ["p1"]},
        ],
    }
    critique_json = {
        "issues": ["Sync worker needs network connectivity check before firing"],
        "phases": [
            {"id": "p1", "title": "Cache DB", "branch": "feat/p1-cache", "prompt": "Create DB table", "depends_on": []},
            {"id": "p2", "title": "Network Monitor", "branch": "feat/p2-net", "prompt": "Monitor connectivity", "depends_on": ["p1"]},
            {"id": "p3", "title": "Sync Worker", "branch": "feat/p3-sync", "prompt": "Run sync worker", "depends_on": ["p2"]},
        ],
    }
    final_json = {
        "tradeoff_summary": "Added network monitor phase to prevent runaway retries on packet loss.",
        "phases": critique_json["phases"],
    }

    # Setup fake runners returning JSON payloads
    def fake_runner_factory(runner_name: str, engine_id: str):
        if runner_name == "gemini":
            return FakeRunner.success("gemini", stdout=json.dumps(draft_json))
        if runner_name == "codex":
            return FakeRunner.success("codex", stdout=f"```json\n{json.dumps(critique_json)}\n```")
        if runner_name == "claude":
            return FakeRunner.success("claude", stdout=json.dumps(final_json))
        return FakeRunner.success(runner_name, stdout="{}")

    council = PlanningCouncil(council_store, council_router, runner_factory=fake_runner_factory)

    with patch.object(council_router, "is_engine_installed", return_value=True):
        session = await council.run_council(
            goal="Add offline status caching with safe network resumption",
            repo_path=tmp_path,
            plan_id="plan-cache-1",
        )

        assert session.plan_id == "plan-cache-1"
        assert len(session.draft.phases) == 2
        assert len(session.critique.phases) == 3
        assert len(session.final.phases) == 3
        assert "prevent runaway retries" in session.final.tradeoffSummary if hasattr(session.final, "tradeoffSummary") else "prevent runaway retries" in session.final.tradeoff_summary

        # Assert all 3 roles persisted in council_runs table
        runs = council_store.list_council_runs(plan_id="plan-cache-1")
        assert len(runs) == 3
        roles_recorded = [r["role"] for r in runs]
        assert roles_recorded == ["draft", "critique", "final"]


@pytest.mark.asyncio
async def test_council_retry_on_invalid_json(
    council_store: Store,
    council_router: Router,
    tmp_path: Path,
) -> None:
    """Verify strict JSON validation retries once with the error message upon malformed JSON."""
    valid_draft = {
        "phases": [
            {"id": "p1", "title": "Phase One", "branch": "feat/p1", "prompt": "Setup", "depends_on": []}
        ]
    }

    call_count = 0

    class RetryFakeRunner:
        name = "gemini"

        async def run(self, prompt: str, workdir: Path, model: str = ""):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt: invalid non-JSON output
                return type("Res", (), {"rc": 0, "stdout": "I think we should do phase 1 first.", "stderr": ""})()
            # Second attempt: received retry prompt, returns valid JSON
            assert "IMPORTANT ERROR FIX" in prompt
            return type("Res", (), {"rc": 0, "stdout": json.dumps(valid_draft), "stderr": ""})()

    def fake_factory(runner: str, eng: str):
        return RetryFakeRunner()

    council = PlanningCouncil(council_store, council_router, runner_factory=fake_factory)

    with patch.object(council_router, "is_engine_installed", return_value=True):
        res = await council.run_draft("Build feature", tmp_path)
        assert call_count == 2
        assert res.retry_count == 1
        assert len(res.phases) == 1
        assert res.phases[0]["id"] == "p1"


@pytest.mark.asyncio
async def test_council_claude_skipped_on_budget_pressure(
    council_store: Store,
    council_router: Router,
    council_ledger: QuotaLedger,
    tmp_path: Path,
) -> None:
    """Verify Claude role is skipped automatically when its budget pressure is high."""
    draft_json = {
        "phases": [
            {"id": "p1", "title": "P1", "branch": "feat/p1", "prompt": "Step 1", "depends_on": []}
        ]
    }

    def fake_factory(runner: str, eng: str):
        return FakeRunner.success(runner, stdout=json.dumps(draft_json))

    council = PlanningCouncil(council_store, council_router, runner_factory=fake_factory)

    # Record 4 of 5 daily runs (80% budget pressure >= 70% threshold)
    for _ in range(4):
        council_ledger.record_usage("claude", "claude-3-7-sonnet", tokens_in=500, tokens_out=400)

    with patch.object(council_router, "is_engine_installed", return_value=True):
        session = await council.run_council("Build goal", tmp_path)
        assert session.final.skipped is True
        assert "Claude skipped" in session.final.skip_reason
        assert "gold quota" in session.final.skip_reason


@pytest.mark.asyncio
async def test_council_role_substitution_when_gemini_unavailable(
    council_store: Store,
    council_router: Router,
    council_ledger: QuotaLedger,
    tmp_path: Path,
) -> None:
    """Verify role substitution through Router when preferred planner is cooling down."""
    draft_json = {
        "phases": [
            {"id": "p1", "title": "P1", "branch": "feat/p1", "prompt": "Step 1", "depends_on": []}
        ]
    }

    def fake_factory(runner: str, eng: str):
        return FakeRunner.success(runner, stdout=json.dumps(draft_json))

    council = PlanningCouncil(council_store, council_router, runner_factory=fake_factory)

    # Set Gemini cooling down
    council_ledger.record_quota_error("gemini", "gemini-2.5-pro", "Rate limit exceeded. Reset in 45m.")

    with patch.object(council_router, "is_engine_installed", return_value=True):
        res = await council.run_draft("Build goal", tmp_path)
        assert res.substituted is True
        assert res.engine_id != "gemini"
        assert "gemini unavailable; substituted with" in res.substitution_reason


@pytest.mark.asyncio
async def test_council_role_substitution_when_gpt_unavailable(
    council_store: Store,
    council_router: Router,
    council_ledger: QuotaLedger,
    tmp_path: Path,
) -> None:
    """Verify critique role substitutes via Router when GPT (codex) is unavailable."""
    critique_json = {
        "issues": ["Add unit test for edge case"],
        "phases": [
            {"id": "p1", "title": "P1", "branch": "feat/p1", "prompt": "Step 1", "depends_on": []}
        ],
    }

    def fake_factory(runner: str, eng: str):
        return FakeRunner.success(runner, stdout=json.dumps(critique_json))

    council = PlanningCouncil(council_store, council_router, runner_factory=fake_factory)
    council_ledger.record_quota_error("codex", "o3-mini", "Usage limit reached. Reset in 30m.")

    with patch.object(council_router, "is_engine_installed", return_value=True):
        res = await council.run_critique("Build goal", {"phases": critique_json["phases"]}, tmp_path)
        assert res.substituted is True
        assert res.engine_id != "codex"
        assert "codex unavailable; substituted with" in res.substitution_reason
        assert len(res.critique_issues) == 1


def test_repo_snapshot_generation(tmp_path: Path) -> None:
    """Verify built-in compact tree and summaries are produced when repomix is absent."""
    # Create sample files
    (tmp_path / "README.md").write_text("# Test Repo\nAutonomous nightly coding.", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'", encoding="utf-8")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("print('hello')", encoding="utf-8")

    snapshot = generate_repo_snapshot(tmp_path)
    assert "README.md" in snapshot
    assert "Autonomous nightly coding." in snapshot
    assert "src/" in snapshot
    assert "main.py" in snapshot
