import json
import threading

from nightshift.bridge import BridgeAPI, attach_event_pump
from nightshift.engine.logins import LoginCheckResult
from nightshift.events import Event, EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store


def test_start_executes_selected_project_plan_and_rejects_duplicate(tmp_path) -> None:
    store = Store(tmp_path / "run.db")
    store.migrate()
    for project_id in ("first", "selected"):
        store.create_project(project_id, project_id, f"owner/{project_id}", str(tmp_path))
        store.create_plan(f"{project_id}-plan", project_id, "Plan")
        store.upsert_phase(f"{project_id}-phase", f"{project_id}-plan", "Phase", "feat/phase")
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Engine:
        is_running = False

        async def execute_plan(self, project, phases, *, plan_id):
            calls.append((project.id, [p.id for p in phases], plan_id))
            entered.set()
            release.wait(5)

    bridge = BridgeAPI(store, EventBus(), ModuleRegistry(), Engine())
    bridge._logins.check_all = lambda **kwargs: {}
    bridge.call("app.selectProject", {"projectId": "selected"})
    assert bridge.call("engine.start")["ok"]
    assert entered.wait(5)
    assert calls == [("selected", ["selected-phase"], "selected-plan")]
    assert not bridge.call("engine.start")["ok"]
    release.set()
    bridge._run_thread.join(5)
    store.close()


def test_snapshot_checks_selected_repository_and_empty_project(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "snapshot.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(), ModuleRegistry())
    calls = []
    monkeypatch.setattr(bridge._logins, "check_all", lambda **kwargs: calls.append(kwargs) or {
        "git": LoginCheckResult(tool="git", state="Logged out", detail="Sign in")
    })
    assert bridge.call("app.snapshot")["data"]["logins"] == {}
    store.create_project("p", "Project", "owner/repo", str(tmp_path))
    snapshot = bridge.call("app.selectProject", {"projectId": "p"})["data"]
    assert snapshot["logins"]["git"]["state"] == "Logged out"
    assert calls == [{"repo": "owner/repo", "force": False}]
    store.close()


def test_event_pump_serializes_and_unsubscribes() -> None:
    bus = EventBus()
    scripts: list[str] = []
    unsubscribe = attach_event_pump(bus, scripts.append)
    bus.emit(Event(kind="phase.parked", plain_text="Needs attention", payload={"title": "</script>"}))
    assert len(scripts) == 1
    serialized = scripts[0].split("{detail: ", 1)[1].rsplit("}));", 1)[0]
    data = json.loads(serialized)
    assert data["kind"] == "phase.parked"
    assert data["payload"]["title"] == "</script>"
    assert "</script>" not in scripts[0]
    unsubscribe()
    bus.emit(Event(kind="other", plain_text="Ignored"))
    assert len(scripts) == 1


def test_unavailable_window_does_not_interrupt_engine() -> None:
    bus = EventBus()

    def unavailable(_script: str) -> None:
        raise RuntimeError("Window closed")

    attach_event_pump(bus, unavailable)
    bus.emit(Event(kind="phase.started", plain_text="Working"))


def test_login_bridge_checks_selected_repository_and_rechecks(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "test.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(), ModuleRegistry())
    calls = []

    def check_all(*, repo, force):
        calls.append((repo, force))
        return {"gh": LoginCheckResult(tool="gh", state="Ready", detail="Verified")}

    monkeypatch.setattr(bridge._logins, "check_all", check_all)
    assert not bridge.call("logins.status")["ok"]
    assert bridge.call("logins.status", {"repo": "owner/project"})["data"]["gh"]["state"] == "Ready"
    bridge.call("logins.recheck", {"repo": "owner/project"})
    assert calls == [("owner/project", False), ("owner/project", True)]
    store.close()


def test_login_fix_uses_allowlisted_argument_list(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "test.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(), ModuleRegistry())
    calls = []
    monkeypatch.setattr("nightshift.bridge.subprocess.Popen", lambda command, **kwargs: calls.append((command, kwargs)))
    assert bridge.call("logins.openFix", {"tool": "gh"})["ok"]
    assert calls[0][0] == ["gh", "auth", "login"]
    assert not bridge.call("logins.openFix", {"tool": "gh; dangerous"})["ok"]
    assert len(calls) == 1
    store.close()


def test_bridge_engine_status_and_stop_controls(tmp_path) -> None:
    store = Store(tmp_path / "test_engine.db")
    store.migrate()
    events = EventBus(store=store)

    class DummyEngine:
        def __init__(self) -> None:
            self.is_running = True
            self.stop_requested = False

        def request_stop(self) -> None:
            self.stop_requested = True

        def cancel_stop(self) -> None:
            self.stop_requested = False

    engine = DummyEngine()
    bridge = BridgeAPI(store, events, ModuleRegistry(), engine=engine)

    status = bridge.call("engine.status")
    assert status["ok"]
    assert status["data"]["running"] is True
    assert status["data"]["runState"] == "running"
    assert status["data"]["stopArmed"] is False

    # Stop after phase
    stop_resp = bridge.call("engine.stopAfterPhase")
    assert stop_resp["ok"]
    assert stop_resp["data"]["armed"] is True
    assert engine.stop_requested is True

    # Undo stop
    undo_resp = bridge.call("engine.undoStop")
    assert undo_resp["ok"]
    assert undo_resp["data"]["armed"] is False
    assert engine.stop_requested is False

    # Cannot start while running
    start_resp = bridge.call("engine.start")
    assert not start_resp["ok"]

    engine.is_running = False
    assert not bridge.call("engine.start")["ok"]

    store.close()


def test_bridge_plan_and_runs_and_engines_list(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "test_data.db")
    store.migrate()
    events = EventBus(store=store)
    bridge = BridgeAPI(store, events, ModuleRegistry())
    monkeypatch.setattr(bridge._logins, "check_all", lambda **kwargs: {
        "gh": LoginCheckResult(tool="gh", state="Ready", detail="Verified")
    })

    store.create_project("p1", "Test Project", "owner/repo", "/path")
    store.create_plan("plan1", "p1", "Overnight Plan")
    store.upsert_phase("ph1", "plan1", "Phase 1", "feat/ph1")

    # plan.get
    plan_resp = bridge.call("plan.get", {"planId": "plan1"})
    assert plan_resp["ok"]
    assert plan_resp["data"]["plan"]["title"] == "Overnight Plan"
    assert len(plan_resp["data"]["phases"]) == 1

    # runs.list
    runs_resp = bridge.call("runs.list")
    assert runs_resp["ok"]
    assert len(runs_resp["data"]) == 1
    assert runs_resp["data"][0]["phasesCount"] == 1

    # engines.list
    eng_resp = bridge.call("engines.list")
    assert eng_resp["ok"]
    assert len(eng_resp["data"]) >= 4

    # app.snapshot
    snap_resp = bridge.call("app.snapshot")
    assert snap_resp["ok"]
    assert snap_resp["data"]["activeProjectId"] == "p1"
    assert len(snap_resp["data"]["projects"]) == 1
    assert snap_resp["data"]["logins"]["gh"]["state"] == "Ready"

    # settings get / set
    assert bridge.call("settings.get", {"key": "theme"})["data"] is None
    set_resp = bridge.call("settings.set", {"key": "theme", "value": "dark"})
    assert set_resp["ok"]
    assert bridge.call("settings.get", {"key": "theme"})["data"] == "dark"

    # plan.validate
    val_resp = bridge.call("plan.validate", {"phases": [{"id": "bad", "branch": "", "title": "Bad"}]})
    assert val_resp["ok"]
    assert val_resp["data"]["valid"] is False
    assert any("needs a branch" in p for p in val_resp["data"]["problems"])

    # plan.save
    save_resp = bridge.call("plan.save", {
        "plan": {"id": "plan2", "project_id": "p1", "title": "Saved Plan"},
        "phases": [
            {"id": "p2-1", "title": "P2-1", "branch": "feat/p2-1", "depends_on": [], "ladder": "ui", "timeout_minutes": 42, "auto_merge": False, "release_after_merge": True, "force_rerun": True},
            {"id": "p2-2", "title": "P2-2", "branch": "feat/p2-2", "depends_on": ["p2-1"]},
        ],
    })
    assert save_resp["ok"]
    assert save_resp["data"]["planId"] == "plan2"

    saved = store.get_phase("p2-1")
    assert (saved["ladder"], saved["timeout_minutes"], saved["auto_merge"], saved["release_after_merge"], saved["force_rerun"]) == ("ui", 42, 0, 1, 1)

    # plan.setImplementer
    impl_resp = bridge.call("plan.setImplementer", {
        "phaseIds": ["p2-1"],
        "engine": "Gemini CLI",
        "model": "gemini-2.5-pro",
        "effort": "high",
    })
    assert impl_resp["ok"]
    assert impl_resp["data"]["updated"] == 1
    assert store.get_phase_assignment("p2-1")["override_engine"] == "gemini"

    # plan.exportQueue
    exp_resp = bridge.call("plan.exportQueue", {"planId": "plan2"})
    assert exp_resp["ok"]
    assert exp_resp["data"]["repo"] == "owner/repo"

    # plan.importQueue
    import_payload = {
        "repo": "owner/imported",
        "local_repo": "/local/imported",
        "phases": [{"id": "imp1", "title": "Imported Phase", "branch": "feat/imp1"}],
    }
    imp_resp = bridge.call("plan.importQueue", {"queue": import_payload})
    assert imp_resp["ok"]
    assert imp_resp["data"]["project"]["id"] == "imported"

    # runs.get
    run_detail = bridge.call("runs.get", {"runId": "plan1"})
    assert run_detail["ok"]
    assert "Morning Digest" in run_detail["data"]["digest"]
    assert len(run_detail["data"]["phases"]) >= 1

    # runs.openArtifactFolder
    folder_resp = bridge.call("runs.openArtifactFolder", {"projectId": "p1"})
    assert folder_resp["ok"]
    assert folder_resp["data"]["opened"] is True

    # engines.getLadders and setLadders (Claude pinned last)
    ladders_resp = bridge.call("engines.getLadders")
    assert ladders_resp["ok"]
    assert "Claude is pinned last" in ladders_resp["data"]["pinnedNote"]

    set_ladders = bridge.call("engines.setLadders", {
        "ladders": {"backend": ["claude", "agy", "gemini"]}  # User put claude first
    })
    assert set_ladders["ok"]
    # Verify claude was automatically pinned last!
    assert set_ladders["data"]["ladders"]["backend"][-1] == "claude"

    # engines.getLedger
    ledger_resp = bridge.call("engines.getLedger")
    assert ledger_resp["ok"]
    assert ledger_resp["data"]["isEstimate"] is True
    assert len(ledger_resp["data"]["chartData"]) == 4

    # engines.update
    eng_up = bridge.call("engines.update", {"id": "agy", "effort": "high"})
    assert eng_up["ok"]

    # settings: projects CRUD
    proj_list = bridge.call("settings.getProjects")
    assert proj_list["ok"]
    assert len(proj_list["data"]) >= 1

    new_proj = bridge.call("settings.saveProject", {
        "id": "new_app",
        "name": "New App",
        "repo": "owner/new-app",
        "local_path": "/path/new-app",
        "build_rules": "gradlew assembleDebug",
    })
    assert new_proj["ok"]
    assert new_proj["data"]["id"] == "new_app"

    del_proj = bridge.call("settings.deleteProject", {"id": "new_app"})
    assert del_proj["ok"]

    # settings: priority list, guard limits, data folder, keep awake
    prio_resp = bridge.call("settings.getPriorityList")
    assert prio_resp["ok"]
    assert len(prio_resp["data"]) == 4

    prio_set = bridge.call("settings.setPriorityList", {
        "items": [{"position": 1, "engine": "gemini", "model": "auto", "effort": "medium"}]
    })
    assert prio_set["ok"]

    guards_get = bridge.call("settings.getGuardLimits")
    assert guards_get["ok"]
    assert guards_get["data"]["maxAttempts"] == 3

    guards_set = bridge.call("settings.setGuardLimits", {"maxAttempts": 5})
    assert guards_set["ok"]
    assert bridge.call("settings.getGuardLimits")["data"]["maxAttempts"] == 5

    data_folder = bridge.call("settings.openDataFolder")
    assert data_folder["ok"]
    assert data_folder["data"]["opened"] is True

    keep_awake = bridge.call("settings.setKeepAwake", {"enabled": True})
    assert keep_awake["ok"]
    assert keep_awake["data"]["keepAwake"] is True

    store.close()

def test_bridge_preserves_phase_execution_and_rejects_cross_plan_ids(tmp_path):
    store = Store(tmp_path / "data.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())
    store.create_project("p", "Project", "owner/repo", "/path")
    store.create_plan("one", "p", "One")
    store.upsert_phase("shared", "one", "Original", "feat/shared", status="merged", attempt=3, pr_number=42)
    saved = bridge.call("plan.save", {"plan": {"id": "one", "project_id": "p"}, "phases": [{"id": "shared", "title": "Edited"}]})
    assert saved["ok"]
    phase = store.get_phase("shared")
    assert (phase["title"], phase["status"], phase["attempt"], phase["pr_number"]) == ("Edited", "merged", 3, 42)
    rejected = bridge.call("plan.save", {"plan": {"id": "two", "project_id": "p"}, "phases": [{"id": "shared", "title": "Wrong"}]})
    assert not rejected["ok"]
    assert store.get_phase("shared")["title"] == "Edited"
    assert store.get_plan("two") is None
    imported = bridge.call("plan.importQueue", {"queue": {"repo": "owner/imported", "phases": [{"id": "shared", "title": "Wrong", "branch": "feat/shared"}]}})
    assert not imported["ok"]
    assert store.get_phase("shared")["title"] == "Edited"


def test_bridge_run_detail_and_persisted_engine_contract(tmp_path):
    store = Store(tmp_path / "data.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())
    store.create_project("p", "Project", "owner/repo", "/path")
    store.create_plan("one", "p", "One")
    store.upsert_phase("phase", "one", "Phase", "feat/phase", status="merged")
    summary = bridge.call("runs.list")["data"][0]
    assert bridge.call("runs.get", {"runId": "one"})["data"]["run"] == summary
    store.upsert_engine("custom", "custom-cli", "model", skills_json='{"backend": 4}', cost_tier="economy", enabled=0)
    engine = bridge.call("engines.list")["data"][0]
    assert (engine["name"], engine["costTier"], engine["state"], engine["quotaUsedPct"], engine["skills"]) == ("custom-cli", "economy", "unavailable", 0, {"backend": 4})


def test_engine_effort_update_preserves_configuration(tmp_path):
    store = Store(tmp_path / "data.db")
    store.migrate()
    bridge = BridgeAPI(store, EventBus(store=store), ModuleRegistry())
    before = store.upsert_engine(
        "custom", "custom-cli", "custom-model", effort="low",
        skills_json='{"backend": 5}', cost_tier="gold", budget_json='{"limit": 42}',
        enabled=0, pinned=1, command_template='["custom", "{instruction}"]',
        usage_parser="custom-parser", quota_patterns_json='["limit"]',
    )
    assert bridge.call("engines.update", {"id": "custom", "effort": "high"})["ok"]
    after = store.get_engine("custom")
    assert after == {**before, "effort": "high"}
    store.close()
