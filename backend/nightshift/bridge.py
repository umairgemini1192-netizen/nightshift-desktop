"""Typed, versioned JS API exposed to the React frontend via pywebview."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict
from typing import Any, Callable

from nightshift import __version__
from nightshift.engine.deck import generate_deck_data
from nightshift.engine.logins import LoginHealthChecker
from nightshift.events import Event, EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.quota.ledger import QuotaLedger
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
        engine: Any | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._registry = registry
        self._engine = engine
        self._logins = LoginHealthChecker(store=store, quota_ledger=QuotaLedger(store))
        self._stop_armed = False

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

        # Engine controls
        if method == "engine.status":
            project_id = str(payload.get("projectId") or payload.get("repo") or "")
            if not project_id:
                projects = self._store.list_projects()
                project_id = projects[0]["id"] if projects else "nightshift-desktop"
            deck = generate_deck_data(self._store, project_id=project_id)
            is_running = bool(self._engine and getattr(self._engine, "is_running", False))
            stop_armed = bool(getattr(self._engine, "stop_requested", self._stop_armed))
            run_state = "running" if is_running else ("failed" if deck.get("needs_you") else "idle")
            return {
                "ok": True,
                "data": {
                    "running": is_running,
                    "runState": run_state,
                    "stopArmed": stop_armed,
                    "deck": deck,
                },
            }

        if method == "engine.start":
            if self._engine and getattr(self._engine, "is_running", False):
                return {"ok": False, "error": "Engine is already running."}
            self._events.emit(Event(
                kind="run.started",
                plain_text="Night-Shift overnight run started.",
                level="info",
            ))
            return {"ok": True, "data": {"started": True}}

        if method == "engine.stopAfterPhase":
            if self._engine and hasattr(self._engine, "request_stop"):
                self._engine.request_stop()
            self._stop_armed = True
            return {
                "ok": True,
                "data": {
                    "armed": True,
                    "message": "Night-Shift will stop after the current phase finishes.",
                },
            }

        if method == "engine.undoStop":
            if self._engine and hasattr(self._engine, "cancel_stop"):
                self._engine.cancel_stop()
            self._stop_armed = False
            return {
                "ok": True,
                "data": {
                    "armed": False,
                    "message": "Stop after current phase cancelled.",
                },
            }

        # Plan inspection
        if method == "plan.get":
            plan_id = str(payload.get("planId") or "")
            if not plan_id:
                plans = self._store.list_plans()
                plan_id = plans[0]["id"] if plans else ""
            plan = self._store.get_plan(plan_id) if plan_id else None
            phases = self._store.list_phases(plan_id) if plan_id else self._store.list_phases()
            return {"ok": True, "data": {"plan": plan, "phases": phases}}

        # Runs history
        if method == "runs.list":
            plans = self._store.list_plans()
            runs = []
            for p in plans:
                phases = self._store.list_phases(p["id"])
                runs.append({
                    "id": p["id"],
                    "projectId": p.get("project_id", ""),
                    "title": p.get("title", ""),
                    "status": p.get("status", "draft"),
                    "phasesCount": len(phases),
                    "phasesDone": len([ph for ph in phases if ph.get("status") in ("merged", "completed")]),
                    "createdAt": p.get("created_at", ""),
                    "updatedAt": p.get("updated_at", ""),
                })
            return {"ok": True, "data": runs}

        # Engines catalogue
        if method == "engines.list":
            engines = self._store.list_engines()
            if not engines:
                engines = [
                    {"id": "agy", "runner": "agy", "model": "auto", "effort": "medium", "state": "ready"},
                    {"id": "gemini", "runner": "gemini", "model": "gemini-2.5-pro", "effort": "medium", "state": "ready"},
                    {"id": "codex", "runner": "codex", "model": "o3-mini", "effort": "medium", "state": "ready"},
                    {"id": "claude", "runner": "claude", "model": "claude-3-7-sonnet", "effort": "medium", "state": "ready"},
                ]
            return {"ok": True, "data": engines}

        # Full app snapshot for desktop UI state
        if method in ("app.snapshot", "app.selectProject"):
            projects = self._store.list_projects()
            active_project_id = str(payload.get("projectId") or "")
            if not active_project_id and projects:
                active_project_id = projects[0]["id"]

            deck = generate_deck_data(self._store, project_id=active_project_id or "nightshift-desktop")
            is_running = bool(self._engine and getattr(self._engine, "is_running", False))
            stop_armed = bool(getattr(self._engine, "stop_requested", self._stop_armed))
            parked = self._store.list_parked()

            run_state = "running" if is_running else ("failed" if deck.get("needs_you") else "idle")
            scenario = "empty" if not projects else ("running" if is_running else ("failure" if parked else "idle"))

            phases = self._store.list_phases()
            done_count = len([ph for ph in phases if ph.get("status") in ("merged", "completed")])

            badges = {
                "mission-control": {
                    "label": f"{len(parked)} parked" if parked else ("Running" if is_running else "Idle"),
                    "tone": "danger" if parked else ("info" if is_running else "neutral"),
                },
                "plan-builder": {
                    "label": f"{done_count} / {len(phases)}" if phases else "Empty",
                    "tone": "info" if is_running else "neutral",
                },
                "engines-quota": {
                    "label": "4 ready",
                    "tone": "success",
                },
            }

            return {
                "ok": True,
                "data": {
                    "scenario": scenario,
                    "runState": run_state,
                    "headline": deck.get("headline", "Night-Shift is ready"),
                    "activeProjectId": active_project_id or None,
                    "projects": [{"id": p["id"], "name": p.get("name", p["id"]), "repo": p["repo"]} for p in projects],
                    "activePhaseTitle": deck.get("now", {}).get("phase"),
                    "activeEngineLabel": deck.get("now", {}).get("engine"),
                    "etaEstimate": deck.get("now", {}).get("eta"),
                    "moduleBadges": badges,
                    "deck": deck,
                    "stopArmed": stop_armed,
                },
            }

        handler = self._registry.resolve(method)
        if handler is None:
            return {"ok": False, "error": f"Unknown bridge method: {method}"}
        return {"ok": True, "data": handler(payload)}
