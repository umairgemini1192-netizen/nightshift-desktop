import json

from nightshift.bridge import BridgeAPI, attach_event_pump
from nightshift.engine.logins import LoginCheckResult
from nightshift.events import Event, EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store


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
