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

    store.close()
