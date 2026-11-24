"""In-process event bus that persists audit events and broadcasts to UI subscribers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from nightshift.store import Store


@dataclass(frozen=True)
class Event:
    """Structured event emitted by engine steps, guards, and modules."""

    kind: str
    plain_text: str
    level: str = "info"
    phase_id: str | None = None
    raw_path: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


Subscriber = Callable[[Event], None]


class EventBus:
    """Pub/sub event bus backed by the append-only SQLite ``events`` table."""

    def __init__(self, store: Store | None = None) -> None:
        self._store = store
        self._subscribers: dict[str, list[Subscriber]] = defaultdict(list)

    def subscribe(self, kind: str, callback: Subscriber) -> Callable[[], None]:
        """Subscribe to a specific event kind or ``'*'`` for all events."""
        self._subscribers[kind].append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers[kind]:
                self._subscribers[kind].remove(callback)

        return unsubscribe

    def emit(self, event: Event) -> int | None:
        """Persist the event (if a store is attached) and notify subscribers."""
        event_id: int | None = None
        if self._store is not None:
            event_id = self._store.append_event(
                level=event.level,
                phase_id=event.phase_id,
                kind=event.kind,
                plain_text=event.plain_text,
                raw_path=event.raw_path,
                ts=event.ts,
            )
        for callback in list(self._subscribers.get(event.kind, [])) + list(self._subscribers.get("*", [])):
            callback(event)
        return event_id
