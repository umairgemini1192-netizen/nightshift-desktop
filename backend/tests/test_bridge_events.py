import json

from nightshift.bridge import attach_event_pump
from nightshift.events import Event, EventBus


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
