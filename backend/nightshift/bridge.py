"""Typed, versioned JS API exposed to the React frontend via pywebview."""

from __future__ import annotations

from typing import Any

from nightshift import __version__
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store


class BridgeAPI:
    """RPC facade exposed as ``window.pywebview.api`` to the frontend."""

    API_VERSION = "1"

    def __init__(
        self,
        store: Store,
        events: EventBus,
        registry: ModuleRegistry,
    ) -> None:
        self._store = store
        self._events = events
        self._registry = registry

    def call(self, method: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch a ``<module>.<action>`` or ``app.<action>`` RPC call."""
        payload = args or {}
        if method == "app.info":
            return {
                "ok": True,
                "data": {
                    "version": __version__,
                    "apiVersion": self.API_VERSION,
                    "modules": self._registry.list_ids(),
                },
            }
        if method == "settings.get":
            key = str(payload.get("key", ""))
            return {"ok": True, "data": self._store.get_setting(key)}
        if method == "settings.set":
            key = str(payload.get("key", ""))
            value = str(payload.get("value", ""))
            self._store.set_setting(key, value)
            return {"ok": True, "data": {"key": key, "value": value}}

        handler = self._registry.resolve(method)
        if handler is None:
            return {"ok": False, "error": f"Unknown bridge method: {method}"}
        return {"ok": True, "data": handler(payload)}
