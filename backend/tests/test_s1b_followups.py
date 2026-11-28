"""Follow-ups to s1b from the Codex review of PR #35 (d3c620b), both P2.

1. A phase merged before the import (historic merge, no delivery) returns SKIPPED from
   ``_finish_merged_delivery``. ``execute_plan`` must count it as a completed dependency;
   otherwise dependants get stacked on an already-merged branch and cannot auto-merge.
2. A runner resolved from configuration (e.g. the required Codex reviewer) must keep the
   engine's configured model when the caller does not pass one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.planner import PhaseDefinition
from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
from nightshift.engine.vcs import FakeGitGh
from nightshift.events import EventBus
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.fake import FakeRunner
from nightshift.runners.template import TemplateRunner
from nightshift.store import Store


def _ok_login_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    return (0, '{"loggedIn": true}\nLogged in to github.com\nOK', "", False)


def test_template_runner_uses_configured_model_when_none_passed(tmp_path: Path) -> None:
    runner = TemplateRunner("codex", ["codex", "exec", "{instruction}", "--model", "{model}"], default_model="o3-mini")
    assert runner.build_command("review", tmp_path) == ["codex", "exec", "review", "--model", "o3-mini"]
    # An explicit model still wins.
    assert runner.build_command("review", tmp_path, model="o4")[-1] == "o4"


def test_resolved_runner_keeps_engine_model(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.db")
    store.migrate()
    store.upsert_engine("codex", "codex", "o3-mini", command_template='["codex", "exec", "{instruction}", "--model", "{model}"]')
    sm = EngineStateMachine(
        store=store, event_bus=EventBus(store), quota_ledger=QuotaLedger(store),
        vcs=FakeGitGh(), runners={"agy": FakeRunner("agy")},
    )
    try:
        runner = sm._resolve_runner("codex")
        assert runner is not None
        assert runner.build_command("review", tmp_path)[-1] == "o3-mini"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_historic_merged_phase_counts_as_completed_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "h.db")
    store.migrate()
    vcs = FakeGitGh()
    vcs.merged_prs["feat/old"] = {"number": 7, "mergeCommit": {"oid": "oldsha7"}, "mergedAt": "2026-11-01T00:00:00Z"}
    store.create_project("owner/repo", "Test", "owner/repo", str(tmp_path))
    store.create_plan("plan-h", "owner/repo", "Plan")
    store.upsert_phase(phase_id="p-old", plan_id="plan-h", title="Old", branch="feat/old", status="merged", pr_number=7)
    store.upsert_phase(phase_id="p-new", plan_id="plan-h", title="New", branch="feat/new", depends_on_json='["p-old"]', position=1)
    quota = QuotaLedger(store)
    sm = EngineStateMachine(
        store=store, event_bus=EventBus(store), quota_ledger=quota,
        vcs=vcs, runners={"agy": FakeRunner("agy")},
        login_checker=LoginHealthChecker(store=store, quota_ledger=quota, command_runner=_ok_login_runner),
    )
    seen: dict[str, Any] = {}

    async def _fake_execute_phase(project: Any, phase: Any, plan_id: str, base_branch: str, *, dependencies_merged: bool) -> dict[str, Any]:
        seen[phase.id] = {"base": base_branch, "deps_merged": dependencies_merged}
        return {"phase_id": phase.id, "title": phase.title, "runner": "none", "status": "COMPLETED", "detail": "fake"}

    monkeypatch.setattr(sm, "_execute_phase", _fake_execute_phase)
    project = ProjectConfig(id="owner/repo", name="Test", repo="owner/repo", local_path=tmp_path, output_dir=tmp_path / "out")
    phases = [
        PhaseDefinition(id="p-old", title="Old", branch="feat/old"),
        PhaseDefinition(id="p-new", title="New", branch="feat/new", depends_on=["p-old"]),
    ]
    try:
        rows = await sm.execute_plan(project, phases, plan_id="plan-h")
        old_row = next(r for r in rows if r["phase_id"] == "p-old")
        assert old_row["status"] == "SKIPPED"
        assert seen["p-new"]["deps_merged"] is True
        assert seen["p-new"]["base"] == "main"
    finally:
        store.close()
