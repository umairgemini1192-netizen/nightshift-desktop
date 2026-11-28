"""Regression tests for S1a UI truth & real-data fixes."""

from __future__ import annotations

import json

from nightshift.bridge import BridgeAPI
from nightshift.engine.logins import LoginCheckResult, LoginHealthChecker
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store


def test_engines_get_ladders_normalizes_object_and_string_entries(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())

    store.set_setting(
        "engines.ladders",
        json.dumps(
            {
                "build": [
                    {"runner": "agy", "model": "gemini-4-neon", "effort": "medium"},
                    {"runner": "gemini", "model": "gemini-2.5-pro"},
                    "claude",
                ]
            }
        ),
    )

    resp = bridge.call("engines.getLadders")
    assert resp["ok"]
    build_ladder = resp["data"]["ladders"]["build"]
    assert build_ladder == [
        {"runner": "agy", "model": "gemini-4-neon", "effort": "medium"},
        {"runner": "gemini", "model": "gemini-2.5-pro", "effort": None},
        {"runner": "claude", "model": None, "effort": None},
    ]
    store.close()


def test_app_log_error_persists_to_event_log(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())

    resp = bridge.call(
        "app.logError",
        {"route": "engines-quota", "title": "AI Engines & Quota", "message": "Objects are not valid as a React child"},
    )
    assert resp["ok"]
    assert resp["data"]["logged"] is True

    events = store.list_events(limit=10)
    assert any(
        e["kind"] == "ui.route_error"
        and "engines-quota" in e["plain_text"]
        and "Objects are not valid as a React child" in e["plain_text"]
        for e in events
    )
    store.close()


def test_fresh_db_returns_no_fabricated_numbers(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "fresh.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())
    monkeypatch.setattr(bridge._logins, "check_all", lambda **kwargs: {})

    ledger_resp = bridge.call("engines.getLedger")
    assert ledger_resp["ok"]
    assert ledger_resp["data"]["chartData"] == []
    assert ledger_resp["data"]["entries"] == []
    assert ledger_resp["data"]["routerDecisions"] == []

    engines_resp = bridge.call("engines.list")
    assert engines_resp["ok"]
    assert all(e["quotaUsedPct"] == 0 for e in engines_resp["data"])

    status_resp = bridge.call("engine.status")
    assert status_resp["ok"]
    deck = status_resp["data"]["deck"]
    assert deck["usage_tonight"] == {}
    assert deck["handoff_count"] == 0
    assert deck["gold_saved_tonight"]["jobs_done"] == 0
    assert deck["gold_saved_tonight"]["cost_saved_usd"] == 0.0
    assert deck["gold_saved_tonight"]["tokens_saved"] == 0
    assert deck["budget_tonight"]["invocations"] == 0
    assert deck["budget_tonight"]["spentUsdEst"] == 0.0
    assert deck["token_savings_tonight"]["totalTokensSaved"] == 0
    assert deck["token_savings_tonight"]["perTool"] == []
    store.close()


def test_runs_list_excludes_imported_draft_plans_and_avoids_fake_durations(tmp_path) -> None:
    store = Store(tmp_path / "runs.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())

    queue = {
        "repo": "owner/app",
        "local_repo": "D:/Projects/app",
        "failover_ladders": {
            "build": [{"runner": "agy", "model": "gemini-4-neon", "effort": "medium"}]
        },
        "phases": [
            {"id": "p1", "title": "Imported Phase 1", "branch": "feat/p1", "ladder": "build"},
            {"id": "p2", "title": "Imported Phase 2", "branch": "feat/p2", "ladder": "build"},
        ],
    }
    imp = bridge.call("plan.importQueue", {"queue": queue})
    assert imp["ok"]

    # Imported plan has never been run -> must NOT appear in runs.list
    runs_resp = bridge.call("runs.list")
    assert runs_resp["ok"]
    assert runs_resp["data"] == []

    # Custom imported ladder 'build' must pass plan.validate
    val_resp = bridge.call("plan.validate", {"phases": imp["data"]["phases"]})
    assert val_resp["ok"]
    assert val_resp["data"]["valid"] is True

    # Once a real step is recorded, the run appears with real runner and '-' duration (no fake '1 h 15 m')
    store.record_step("p1", "implement", "agy")
    runs_after = bridge.call("runs.list")
    assert runs_after["ok"]
    assert len(runs_after["data"]) == 1
    assert runs_after["data"][0]["duration"] == "-"
    assert runs_after["data"][0]["aiUsed"] == ["agy"]

    detail = bridge.call("runs.get", {"runId": runs_after["data"][0]["id"]})
    assert detail["ok"]
    assert detail["data"]["artifacts"] == []
    assert detail["data"]["phases"][0]["runner"] == "agy"
    assert detail["data"]["phases"][0]["duration"] == "-"
    store.close()


def test_snapshot_badge_matches_selected_plan_and_headline_reflects_loaded_plan(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "snap.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())
    monkeypatch.setattr(bridge._logins, "check_all", lambda **kwargs: {})

    store.create_project("proj1", "DriveLift", "owner/drivelift", "D:/Projects/drivelift")
    store.create_plan("plan_a", "proj1", "Plan A")
    store.upsert_phase("p1", "plan_a", "Phase 1", "feat/p1")
    store.upsert_phase("p2", "plan_a", "Phase 2", "feat/p2")

    # Second plan in same project should not inflate selected plan's badge count
    store.create_plan("plan_b", "proj1", "Plan B")
    store.upsert_phase("p3", "plan_b", "Phase 3", "feat/p3")

    snap = bridge.call("app.snapshot")
    assert snap["ok"]
    assert snap["data"]["moduleBadges"]["plan-builder"]["label"] == "0 / 2"
    assert "Select a plan to start" not in snap["data"]["headline"]
    store.close()


def test_login_readiness_disabled_by_owner_and_gemini_no_signin(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "logins.db")
    store.migrate()
    store.upsert_engine("claude", "claude", "claude-3-7-sonnet", enabled=0)
    store.upsert_engine("gemini", "gemini", "gemini-2.5-pro", enabled=1)

    probed: list[list[str]] = []

    def fake_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
        probed.append(cmd)
        return 0, "ok", "", False

    checker = LoginHealthChecker(store=store, command_runner=fake_runner)
    claude_res = checker.check_tool("claude", repo="owner/repo", force=True)
    assert claude_res.state == "Disabled by owner"
    assert probed == []

    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())
    popen_calls: list[list[str]] = []
    monkeypatch.setattr("nightshift.bridge.subprocess.Popen", lambda cmd, **kwargs: popen_calls.append(cmd))

    # Disabled engine refuses Fix/login
    fix_claude = bridge.call("logins.openFix", {"tool": "claude"})
    assert not fix_claude["ok"]
    assert "disabled by owner" in fix_claude["error"].lower()

    # Gemini CLI is API-key only: never start a sign-in
    fix_gemini = bridge.call("logins.openFix", {"tool": "gemini"})
    assert not fix_gemini["ok"]
    assert "api key" in fix_gemini["error"].lower()
    assert popen_calls == []

    # Interactive CLI resolves via resolve_bin (same as runners)
    monkeypatch.setattr("nightshift.bridge.resolve_bin", lambda name: r"C:\Users\owner\AppData\Roaming\npm\codex.cmd")
    fix_codex = bridge.call("logins.openFix", {"tool": "codex"})
    assert fix_codex["ok"]
    assert popen_calls == [[r"C:\Users\owner\AppData\Roaming\npm\codex.cmd", "login"]]
    store.close()
