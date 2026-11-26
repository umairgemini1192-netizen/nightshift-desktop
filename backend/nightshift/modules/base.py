"""Base class for pluggable backend modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nightshift.events import Event


class ModuleBase:
    """Base contract implemented by every backend feature module."""

    id: str = ""

    def api(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        """Return RPC handlers exposed as ``<id>.<name>`` via the bridge."""
        return {}

    def configure(
        self,
        *,
        store: Any = None,
        events: Any = None,
        creds: Any = None,
        engine: Any = None,
    ) -> None:
        """Optional hook called when the bridge attaches core services."""
        _ = (store, events, creds, engine)

    def on_event(self, event: Event) -> None:
        """Optional hook called when events are emitted on the event bus."""
        _ = event

    def stop(self) -> None:
        """Optional hook called when the application shuts down."""
