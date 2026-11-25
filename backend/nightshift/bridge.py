"""Typed, versioned JS API exposed to the React frontend via pywebview."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict
from typing import Any, Callable

from nightshift import __version__
from nightshift.events import Event, EventBus
from nightshift.engine.logins import LoginHealthChecker
from nightshift.quota.ledger import QuotaLedger
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store

logger = logging.getLogger(__name__)


def attach_event_pump(events: EventBus, evaluate_js: Callable[[str], Any]) -> Callable[[], None]:
    """Forward persisted events to the frontend as data, never as executable code."""
    def forward(event: Event) -> None:
        payload = json.dumps(asdict(event), ensure_ascii=True).replace("<", "\\u003c")
        try:
            evaluate_js(f"window.dispatchEvent(new CustomEvent('nightshift:event', {{detail: {payload}}}));")
        except Exception:
            # A window that is loading or closing must not interrupt engine work.
            logger.exception("Could not deliver an event to the window")

    return events.subscribe("*", forward)


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
        self._logins = LoginHealthChecker(store=store, quota_ledger=QuotaLedger(store))

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

        if method in ("logins.status", "logins.recheck"):
            repo = str(payload.get("repo", ""))
            if not repo:
                return {"ok": False, "error": "Select a project before checking logins."}
            checks = self._logins.check_all(repo=repo, force=method == "logins.recheck")
            return {"ok": True, "data": {name: asdict(result) for name, result in checks.items()}}
        if method == "logins.openFix":
            commands = {
                "gh": ["gh", "auth", "login"],
                "claude": ["claude", "auth", "login"],
                "codex": ["codex", "login"],
                "gemini": ["gemini"],
                "agy": ["agy"],
            }
            command = commands.get(str(payload.get("tool", "")))
            if command is None:
                return {"ok": False, "error": "No login command is available for this tool."}
            try:
                subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            except OSError:
                return {"ok": False, "error": "Could not open the login command. Check that the tool is installed."}
            return {"ok": True, "data": {"opened": True}}

        handler = self._registry.resolve(method)
        if handler is None:
            return {"ok": False, "error": f"Unknown bridge method: {method}"}
        return {"ok": True, "data": handler(payload)}
