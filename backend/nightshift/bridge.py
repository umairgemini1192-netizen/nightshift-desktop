"""Typed, versioned JS API exposed to the React frontend via pywebview."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from nightshift import __version__
from nightshift.autostart import AutostartManager
from nightshift.engine.council import CouncilRoleResult, CouncilSessionResult, PlanningCouncil
from nightshift.engine.credentials import CredentialStore, get_credential_store
from nightshift.engine.deck import generate_deck_data
from nightshift.engine.logins import LoginHealthChecker
from nightshift.engine.planner import (
    KNOWN_LADDERS,
    PhaseDefinition,
    export_queue_json,
    import_queue_json,
    validate_plan,
    validate_plan_dag,
)
from nightshift.engine.router import Router, test_engine_cli_version
from nightshift.engine.state_machine import ProjectConfig, set_keep_awake
from nightshift.events import Event, EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.base import resolve_bin
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


ENGINE_NAME_TO_ID: dict[str, str] = {
    "Antigravity": "agy",
    "Gemini CLI": "gemini",
    "Codex CLI": "codex",
    "Claude Code": "claude",
    "Qwen Code": "qwen",
    "DeepSeek": "deepseek",
    "Kimi K2": "kimi",
    "GLM": "glm",
    "MiniMax": "minimax",
}


class BridgeAPI:
    """RPC facade exposed as ``window.pywebview.api`` to the frontend."""

    API_VERSION = "1"

    def __init__(
        self,
        store: Store,
        events: EventBus,
        registry: ModuleRegistry,
        engine: Any | None = None,
        creds: CredentialStore | None = None,
        autostart: AutostartManager | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._registry = registry
        self._engine = engine
        self._quota = QuotaLedger(store)
        self._logins = LoginHealthChecker(store=store, quota_ledger=self._quota)
        self._creds = creds if creds is not None else get_credential_store()
        self._autostart = autostart if autostart is not None else AutostartManager()
        self._window: Any | None = None
        self._maximized = False
        self._router: Router | None = None
        self._stop_armed = False
        self._active_project_id: str | None = None
        self._run_lock = threading.Lock()
        self._run_thread: threading.Thread | None = None
        self._registry.attach(store=self._store, events=self._events, creds=self._creds, engine=self._engine)

    def attach_window(self, window: Any) -> None:
        """Attach the active pywebview window for frameless titlebar controls."""
        self._window = window

    @staticmethod
    def _extract_project_evidence_kwargs(p_data: dict[str, Any]) -> dict[str, Any]:
        """Extract optional Phase 8d evidence configuration columns from a bridge/queue payload."""
        out: dict[str, Any] = {}
        if "required_ci_jobs" in p_data:
            raw_jobs = p_data["required_ci_jobs"]
            if raw_jobs is None:
                out["required_ci_jobs_json"] = None
            elif isinstance(raw_jobs, list):
                out["required_ci_jobs_json"] = json.dumps([str(j) for j in raw_jobs])
            elif isinstance(raw_jobs, str):
                parsed = json.loads(raw_jobs)
                if not isinstance(parsed, list):
                    raise ValueError("required_ci_jobs must be a JSON array")
                out["required_ci_jobs_json"] = json.dumps([str(j) for j in parsed])
            else:
                raise TypeError("required_ci_jobs must be a list, JSON array string, or null")
        elif "required_ci_jobs_json" in p_data:
            raw_json = p_data["required_ci_jobs_json"]
            if raw_json is None or raw_json == "":
                out["required_ci_jobs_json"] = None
            else:
                parsed = json.loads(str(raw_json))
                if not isinstance(parsed, list):
                    raise ValueError("required_ci_jobs_json must encode a JSON array")
                out["required_ci_jobs_json"] = json.dumps([str(j) for j in parsed])

        if "required_artifacts_by_job" in p_data:
            raw_arts = p_data["required_artifacts_by_job"]
            if raw_arts is None:
                out["required_artifacts_by_job_json"] = "{}"
            elif isinstance(raw_arts, dict):
                out["required_artifacts_by_job_json"] = json.dumps(
                    {str(k): [str(v) for v in vals] for k, vals in raw_arts.items() if isinstance(vals, list)}
                )
            elif isinstance(raw_arts, str):
                parsed = json.loads(raw_arts)
                if not isinstance(parsed, dict):
                    raise ValueError("required_artifacts_by_job must be a JSON object")
                out["required_artifacts_by_job_json"] = json.dumps(
                    {str(k): [str(v) for v in vals] for k, vals in parsed.items() if isinstance(vals, list)}
                )
            else:
                raise TypeError("required_artifacts_by_job must be a dict or JSON object string")
        elif "required_artifacts_by_job_json" in p_data:
            raw_json = p_data["required_artifacts_by_job_json"]
            parsed = json.loads(str(raw_json or "{}"))
            if not isinstance(parsed, dict):
                raise ValueError("required_artifacts_by_job_json must encode a JSON object")
            out["required_artifacts_by_job_json"] = json.dumps(
                {str(k): [str(v) for v in vals] for k, vals in parsed.items() if isinstance(vals, list)}
            )

        if "min_gate_duration_seconds" in p_data and p_data["min_gate_duration_seconds"] is not None:
            out["min_gate_duration_seconds"] = float(p_data["min_gate_duration_seconds"])
        if "min_build_duration_seconds" in p_data and p_data["min_build_duration_seconds"] is not None:
            out["min_build_duration_seconds"] = float(p_data["min_build_duration_seconds"])
        if "screenshot_manifest_path" in p_data and p_data["screenshot_manifest_path"] is not None:
            out["screenshot_manifest_path"] = str(p_data["screenshot_manifest_path"])
        if "screenshot_job_name" in p_data and p_data["screenshot_job_name"] is not None:
            out["screenshot_job_name"] = str(p_data["screenshot_job_name"])
        if "smoke_command" in p_data and p_data["smoke_command"] is not None:
            out["smoke_command"] = str(p_data["smoke_command"])
        if "smoke_timeout_seconds" in p_data and p_data["smoke_timeout_seconds"] is not None:
            out["smoke_timeout_seconds"] = int(p_data["smoke_timeout_seconds"])
        return out

    def _enrich_phase_with_assignment(self, ph: dict[str, Any]) -> dict[str, Any]:
        assignment = self._store.get_phase_assignment(str(ph["id"]))
        item = dict(ph)
        if assignment:
            if assignment.get("planned_engine"):
                item["implementer"] = assignment["planned_engine"]
            if assignment.get("override_engine"):
                item["overrideImplementer"] = assignment["override_engine"]
            if assignment.get("model"):
                item["overrideModel"] = assignment["model"]
            if assignment.get("effort"):
                item["overrideEffort"] = assignment["effort"]
        return item

    def _upsert_planned_engine(self, phase_id: str, raw_implementer: Any) -> None:
        if raw_implementer is None:
            return
        assignment = self._store.get_phase_assignment(phase_id) or {}
        raw_planned = str(raw_implementer).strip()
        planned = ENGINE_NAME_TO_ID.get(raw_planned, raw_planned)
        planned_engine = None if planned.lower() == "auto" else (planned or assignment.get("planned_engine"))
        self._store.upsert_phase_assignment(
            phase_id,
            planned_engine=planned_engine,
            override_engine=assignment.get("override_engine"),
            model=assignment.get("model"),
            effort=assignment.get("effort") or "medium",
        )

    def _get_router(self) -> Router:
        if self._router is None:
            self._router = Router(
                self._store,
                self._quota,
                login_checker=self._logins,
                cred_store=self._creds,
            )
        return self._router

    @staticmethod
    def _serialize_council_role(res: CouncilRoleResult) -> dict[str, Any]:
        names = {
            "agy": "Antigravity",
            "gemini": "Gemini CLI",
            "codex": "Codex CLI",
            "claude": "Claude Code",
            "qwen": "Qwen Code",
            "deepseek": "DeepSeek",
            "kimi": "Kimi K2",
            "glm": "GLM",
            "minimax": "MiniMax",
        }
        tokens = int(res.usage_estimate.get("tokens_in", 0)) + int(res.usage_estimate.get("tokens_out", 0))
        return {
            "role": res.role,
            "engineId": res.engine_id,
            "name": names.get(res.runner, res.engine_id),
            "model": res.model,
            "state": "skipped" if res.skipped else "done",
            "substituted": res.substituted,
            "substitutionReason": res.substitution_reason or None,
            "elapsedSeconds": res.elapsed_seconds,
            "tokensEstimate": tokens,
            "costUsdEstimate": float(res.usage_estimate.get("cost_usd", 0.0)),
            "notes": list(res.notes),
            "issues": list(res.critique_issues) if res.critique_issues else None,
            "tradeoffSummary": res.tradeoff_summary or None,
            "phases": list(res.phases),
        }

    def _serialize_council_session(self, session: CouncilSessionResult) -> dict[str, Any]:
        return {
            "goal": session.goal,
            "planId": session.plan_id,
            "diffSummary": session.diff_summary,
            "tradeoffSummary": session.final.tradeoff_summary or session.diff_summary,
            "roles": {
                "draft": self._serialize_council_role(session.draft),
                "critique": self._serialize_council_role(session.critique),
                "final": self._serialize_council_role(session.final),
            },
        }

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
        if method == "app.firstRunStatus":
            dismissed = self._store.get_setting("onboarding.completed") == "1"
            has_projects = len(self._store.list_projects()) > 0
            return {
                "ok": True,
                "data": {
                    "isFirstRun": (not dismissed) and (not has_projects),
                    "dismissed": dismissed,
                    "hasProjects": has_projects,
                },
            }
        if method == "app.dismissFirstRun":
            self._store.set_setting("onboarding.completed", "1")
            return {"ok": True, "data": {"dismissed": True}}
        if method == "app.logError":
            route = str(payload.get("route") or "unknown")
            message = str(payload.get("message") or "Unknown UI error")
            self._events.emit(
                Event(
                    kind="ui.route_error",
                    plain_text=f"UI error on {route}: {message}",
                    level="error",
                    payload={"route": route, "message": message},
                )
            )
            return {"ok": True, "data": {"logged": True}}
        if method == "window.minimize":
            if self._window is not None and hasattr(self._window, "minimize"):
                self._window.minimize()
            return {"ok": True, "data": {"minimized": True}}
        if method == "window.toggleMaximize":
            if self._window is not None:
                if self._maximized and hasattr(self._window, "restore"):
                    self._window.restore()
                    self._maximized = False
                elif hasattr(self._window, "maximize"):
                    self._window.maximize()
                    self._maximized = True
            return {"ok": True, "data": {"maximized": self._maximized}}
        if method == "window.close":
            if self._window is not None and hasattr(self._window, "destroy"):
                self._window.destroy()
            return {"ok": True, "data": {"closed": True}}
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
            tool = str(payload.get("tool", "")).strip()
            eng = self._store.get_engine(tool) if tool else None
            matching = [
                e for e in self._store.list_engines()
                if str(e.get("id") or "") == tool or str(e.get("runner") or "") == tool
            ] if tool else []
            is_disabled = (eng is not None and not int(eng.get("enabled", 1))) or (
                bool(matching) and all(not int(e.get("enabled", 1)) for e in matching)
            )
            if is_disabled:
                return {"ok": False, "error": f"{tool} is disabled by owner."}
            if tool == "gemini":
                return {
                    "ok": False,
                    "error": "Gemini CLI uses an API key configured in AI Engines & Quota; interactive sign-in is disabled.",
                }
            commands = {
                "gh": ["gh", "auth", "login"],
                "claude": ["claude", "auth", "login"],
                "codex": ["codex", "login"],
                "agy": ["agy"],
                "qwen": ["qwen"],
            }
            command = commands.get(tool)
            if command is None:
                return {"ok": False, "error": "No login command is available for this tool."}
            exe = resolve_bin(command[0])
            if not exe:
                return {"ok": False, "error": "Could not open the login command. Check that the tool is installed."}
            if exe.lower().endswith(".ps1"):
                ps = shutil.which("powershell.exe") or "powershell.exe"
                launch_cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe] + command[1:]
            else:
                launch_cmd = [exe] + command[1:]
            try:
                subprocess.Popen(launch_cmd, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
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
            with self._run_lock:
                if (self._run_thread and self._run_thread.is_alive()) or (self._engine and getattr(self._engine, "is_running", False)):
                    return {"ok": False, "error": "Engine is already running."}
                if not self._engine:
                    return {"ok": False, "error": "The engine is unavailable."}
                projects = self._store.list_projects()
                project_id = str(payload.get("projectId") or self._active_project_id or (projects[0]["id"] if projects else ""))
                project = self._store.get_project(project_id)
                plan_id = str(payload.get("planId") or "")
                plans = self._store.list_plans(project_id) if project else []
                plan = self._store.get_plan(plan_id) if plan_id else (plans[0] if plans else None)
                if not project or not plan or plan["project_id"] != project_id:
                    return {"ok": False, "error": "Select a project with a plan before starting."}
                try:
                    phases = [PhaseDefinition.from_dict(p) for p in payload.get("phases", self._store.list_phases(plan["id"]))]
                    if not phases:
                        return {"ok": False, "error": "Add a phase to the plan before starting."}
                    validate_plan_dag(phases)
                    config = ProjectConfig.from_dict(project)
                except (ValueError, TypeError, KeyError) as exc:
                    return {"ok": False, "error": f"The plan could not be started: {exc}"}

                def run() -> None:
                    try:
                        asyncio.run(self._engine.execute_plan(config, phases, plan_id=plan["id"]))
                    except Exception:
                        logger.exception("Plan execution failed")
                        self._events.emit(Event(kind="run.failed", plain_text="The run could not continue.", level="error"))

                self._run_thread = threading.Thread(target=run, daemon=True, name="NightShiftPlan")
                self._run_thread.start()
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

        # Plan inspection, save, validation, and queue import/export
        if method == "plan.get":
            plan_id = str(payload.get("planId") or "")
            if not plan_id:
                target_project_id = str(payload.get("projectId") or self._active_project_id or "")
                project_plans = self._store.list_plans(target_project_id) if target_project_id else []
                plans = project_plans or self._store.list_plans()
                plan_id = plans[0]["id"] if plans else ""
            plan = self._store.get_plan(plan_id) if plan_id else None
            phases = self._store.list_phases(plan_id) if plan_id else self._store.list_phases()
            enriched_phases = [self._enrich_phase_with_assignment(ph) for ph in phases]
            return {"ok": True, "data": {"plan": plan, "phases": enriched_phases}}

        if method == "plan.save":
            plan_data = payload.get("plan") or {}
            plan_id = str(plan_data.get("id") or "default_plan")
            existing_plan = self._store.get_plan(plan_id)
            projects = self._store.list_projects()
            project_id = str(
                plan_data.get("project_id")
                or (existing_plan["project_id"] if existing_plan else "")
                or self._active_project_id
                or (projects[0]["id"] if projects else "default")
            )
            title = str(plan_data.get("title") or (existing_plan["title"] if existing_plan else "Plan"))
            goal = str(
                plan_data.get("goal")
                if "goal" in plan_data and plan_data.get("goal") is not None
                else (existing_plan["goal"] if existing_plan else "")
            )
            phases_data = payload.get("phases") or []
            for phase in phases_data:
                existing = self._store.get_phase(str(phase["id"]))
                if existing and existing["plan_id"] != plan_id:
                    return {"ok": False, "error": f"Phase ID {phase['id']} belongs to another plan."}
            if not self._store.get_project(project_id):
                self._store.create_project(project_id, project_id, "owner/repo", ".", artifact_glob="*.apk")
            self._store.create_plan(plan_id, project_id, title, goal)
            incoming_ids = {str(p["id"]) for p in phases_data}
            for old_ph in self._store.list_phases(plan_id):
                if str(old_ph["id"]) not in incoming_ids:
                    self._store.delete_phase(str(old_ph["id"]))
            for idx, p in enumerate(phases_data):
                raw_deps = p.get("depends_on") or []
                deps_json = json.dumps(raw_deps) if isinstance(raw_deps, list) else str(raw_deps)
                existing = self._store.get_phase(str(p["id"])) or {}
                self._store.upsert_phase(
                    phase_id=str(p["id"]),
                    plan_id=plan_id,
                    title=str(p.get("title", p["id"])),
                    branch=str(p.get("branch", f"feat/{p['id']}")),
                    prompt=str(p.get("prompt", "")),
                    depends_on_json=deps_json,
                    size=str(p.get("size", "medium")),
                    position=int(p.get("position", idx)),
                    status=existing.get("status", "pending"),
                    attempt=existing.get("attempt", 0),
                    pr_number=existing.get("pr_number"),
                    ladder=str(p.get("ladder", "default")), timeout_minutes=int(p.get("timeout_minutes", 180)),
                    auto_merge=bool(p.get("auto_merge", True)), release_after_merge=bool(p.get("release_after_merge", False)),
                    force_rerun=bool(p.get("force_rerun", False)),
                )
                self._upsert_planned_engine(str(p["id"]), p.get("implementer"))
            return {"ok": True, "data": {"saved": True, "planId": plan_id, "projectId": project_id}}

        if method == "plan.validate":
            raw_phases = payload.get("phases")
            if raw_phases is None:
                plan_id = str(payload.get("planId") or "")
                raw_phases = self._store.list_phases(plan_id) if plan_id else self._store.list_phases()
            phase_defs: list[PhaseDefinition] = []
            for p in raw_phases:
                try:
                    phase_defs.append(PhaseDefinition.from_dict(p) if isinstance(p, dict) else p)
                except Exception as e:
                    return {"ok": True, "data": {"valid": False, "problems": [f"Malformed phase: {e}"]}}
            ladders_raw = self._store.get_setting("engines.ladders")
            custom_ladders: set[str] = set()
            if ladders_raw:
                try:
                    parsed_ladders = json.loads(ladders_raw)
                    if isinstance(parsed_ladders, dict):
                        custom_ladders = {str(k) for k in parsed_ladders.keys()}
                except Exception:
                    pass
            problems = validate_plan(phase_defs, known_ladders=KNOWN_LADDERS | custom_ladders)
            return {"ok": True, "data": {"valid": len(problems) == 0, "problems": problems}}

        if method == "plan.importQueue":
            queue_payload = payload.get("queue")
            if not queue_payload:
                return {"ok": False, "error": "No queue data provided to import."}
            try:
                raw_cfg = (
                    json.loads(queue_payload.strip().lstrip("\ufeff"))
                    if isinstance(queue_payload, str)
                    else dict(queue_payload)
                )
                imported = import_queue_json(raw_cfg)
                for ph in imported["phases"]:
                    existing = self._store.get_phase(ph.id)
                    if existing and existing["plan_id"] != imported["plan"]["id"]:
                        return {"ok": False, "error": f"Phase ID {ph.id} belongs to another plan."}
                proj = imported["project"]
                evidence_kwargs = self._extract_project_evidence_kwargs(proj)
                if not self._store.get_project(proj["id"]):
                    proj = self._store.create_project(
                        proj["id"],
                        proj["name"],
                        proj["repo"],
                        proj["local_path"],
                        build_rules=proj.get("build_rules", ""),
                        artifact_glob=proj.get("artifact_glob", "*.apk"),
                        output_dir=proj.get("output_dir", ""),
                        **evidence_kwargs,
                    )
                else:
                    updates: dict[str, Any] = {}
                    if "name" in raw_cfg:
                        updates["name"] = proj["name"]
                    if "local_repo" in raw_cfg:
                        updates["local_path"] = proj["local_path"]
                    if "build_rules" in raw_cfg:
                        updates["build_rules"] = proj.get("build_rules", "")
                    if "artifact_glob" in raw_cfg:
                        updates["artifact_glob"] = proj.get("artifact_glob", "*.apk")
                    if "desktop_apk_dir" in raw_cfg:
                        updates["output_dir"] = proj.get("output_dir", "")
                    updates.update(evidence_kwargs)
                    proj = (
                        self._store.update_project(proj["id"], **updates)
                        if updates
                        else (self._store.get_project(proj["id"]) or proj)
                    )
                plan = imported["plan"]
                self._store.create_plan(plan["id"], proj["id"], plan["title"], plan.get("goal", ""))
                self._active_project_id = proj["id"]
                imported_ids = {ph.id for ph in imported["phases"]}
                for old_ph in self._store.list_phases(plan["id"]):
                    if str(old_ph["id"]) not in imported_ids:
                        self._store.delete_phase(str(old_ph["id"]))
                raw_phases = raw_cfg.get("phases") if isinstance(raw_cfg.get("phases"), list) else []
                for idx, ph in enumerate(imported["phases"]):
                    existing = self._store.get_phase(ph.id) or {}
                    self._store.upsert_phase(
                        phase_id=ph.id,
                        plan_id=plan["id"],
                        status=existing.get("status", "pending"),
                        attempt=existing.get("attempt", 0),
                        pr_number=existing.get("pr_number"),
                        title=ph.title,
                        branch=ph.branch,
                        prompt=ph.prompt,
                        depends_on_json=json.dumps(ph.depends_on),
                        size=ph.size,
                        position=idx, ladder=ph.ladder, timeout_minutes=ph.timeout_minutes,
                        auto_merge=ph.auto_merge, release_after_merge=ph.release_after_merge, force_rerun=ph.force_rerun,
                    )
                    raw_ph = raw_phases[idx] if idx < len(raw_phases) and isinstance(raw_phases[idx], dict) else {}
                    self._upsert_planned_engine(ph.id, raw_ph.get("implementer"))
                if imported.get("ladders"):
                    self._store.set_setting("engines.ladders", json.dumps(imported["ladders"]))
                self._store.set_setting("onboarding.completed", "1")
                return {
                    "ok": True,
                    "data": {
                        "project": proj,
                        "plan": plan,
                        "phases": [self._enrich_phase_with_assignment(ph.to_dict()) for ph in imported["phases"]],
                        "ladders": imported["ladders"],
                    },
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to import queue.json: {e}"}

        if method == "plan.exportQueue":
            plan_id = str(payload.get("planId") or "")
            target_project_id = str(payload.get("projectId") or self._active_project_id or "")
            project_plans = self._store.list_plans(target_project_id) if target_project_id else []
            plans = project_plans or self._store.list_plans()
            if plan_id:
                plan = self._store.get_plan(plan_id) or {
                    "id": plan_id,
                    "title": "Exported Plan",
                    "project_id": target_project_id or "default",
                }
            else:
                plan = plans[0] if plans else {
                    "id": "default",
                    "title": "Exported Plan",
                    "project_id": target_project_id or "default",
                }
            project = (
                self._store.get_project(str(plan.get("project_id") or target_project_id or ""))
                or {"repo": "owner/repo", "local_path": "", "output_dir": "", "artifact_glob": "*.apk"}
            )
            phases = [self._enrich_phase_with_assignment(ph) for ph in self._store.list_phases(plan.get("id"))]
            ladders_raw = self._store.get_setting("engines.ladders")
            ladders = json.loads(ladders_raw) if ladders_raw else {
                "backend": ["agy", "gemini", "codex", "claude"],
                "ui": ["agy", "gemini", "claude"],
                "review": ["gemini", "codex", "claude"],
                "default": ["agy", "gemini", "codex", "claude"],
            }
            exported = export_queue_json(project, plan, phases, ladders)
            return {"ok": True, "data": exported}

        if method == "plan.setImplementer":
            phase_ids = payload.get("phaseIds") or []
            engine = str(payload.get("engine") or "")
            engine = ENGINE_NAME_TO_ID.get(engine, engine)
            model = str(payload.get("model") or "")
            effort = str(payload.get("effort") or "medium")
            reset = bool(payload.get("reset", False))
            for pid in phase_ids:
                existing = self._store.get_phase_assignment(pid) or {}
                planned = existing.get("planned_engine")
                if reset:
                    self._store.upsert_phase_assignment(pid, planned_engine=planned, override_engine=None, model=None, effort="medium")
                else:
                    self._store.upsert_phase_assignment(pid, planned_engine=planned, override_engine=engine, model=model, effort=effort)
            if phase_ids:
                action_text = (
                    f"Reset implementer to plan choice for {len(phase_ids)} phase(s); applies from the next phase."
                    if reset
                    else f"Set implementer to {engine} ({effort} effort) for {len(phase_ids)} phase(s); applies from the next phase."
                )
                self._events.emit(Event(kind="phase.assignment_updated", plain_text=action_text, level="info"))
            return {"ok": True, "data": {"updated": len(phase_ids)}}

        # AI Planning Council (BLUEPRINT 5b)
        if method == "council.run":
            goal = str(payload.get("goal") or "").strip()
            if not goal:
                return {"ok": False, "error": "Describe what you want built before running the Planning Council."}
            projects = self._store.list_projects()
            project_id = str(payload.get("projectId") or self._active_project_id or (projects[0]["id"] if projects else ""))
            project = self._store.get_project(project_id) if project_id else None
            repo_path = Path(project["local_path"]) if project and project.get("local_path") else Path(".")
            plan_id = str(payload.get("planId") or "") or None
            council = PlanningCouncil(self._store, self._get_router())
            session = asyncio.run(
                council.run_council(goal=goal, repo_path=repo_path, plan_id=plan_id, project_id=project_id or None)
            )
            self._events.emit(
                Event(
                    kind="council.completed",
                    plain_text=f"Planning Council produced {len(session.approved_plan.get('phases', []))} phase(s) for review.",
                    level="info",
                )
            )
            return {"ok": True, "data": self._serialize_council_session(session)}

        if method == "council.session":
            plan_id = str(payload.get("planId") or "")
            runs = self._store.list_council_runs(plan_id=plan_id or None)
            if not runs:
                return {"ok": True, "data": None}
            target_plan_id = plan_id or str(runs[-1]["plan_id"])
            plan_runs = [r for r in runs if str(r["plan_id"]) == target_plan_id]
            by_role = {r["role"]: r for r in plan_runs}
            plan_row = self._store.get_plan(target_plan_id) or {}

            def _role_payload(role_name: str, default_eng: str) -> dict[str, Any]:
                row = by_role.get(role_name)
                if not row:
                    return {
                        "role": role_name,
                        "engineId": default_eng,
                        "name": default_eng,
                        "model": "",
                        "state": "waiting",
                        "substituted": False,
                        "elapsedSeconds": 0,
                        "tokensEstimate": 0,
                        "costUsdEstimate": 0.0,
                        "notes": [],
                        "phases": [],
                    }
                try:
                    out_obj = json.loads(row.get("output") or "{}")
                except Exception:
                    out_obj = {}
                try:
                    usage_obj = json.loads(row.get("usage_estimate") or "{}")
                except Exception:
                    usage_obj = {}
                tokens = int(usage_obj.get("tokens_in", 0)) + int(usage_obj.get("tokens_out", 0))
                return {
                    "role": role_name,
                    "engineId": row["engine"],
                    "name": row["engine"],
                    "model": "",
                    "state": "done",
                    "substituted": False,
                    "elapsedSeconds": 0,
                    "tokensEstimate": tokens,
                    "costUsdEstimate": float(usage_obj.get("cost_usd", 0.0)),
                    "notes": [out_obj.get("tradeoff_summary")] if out_obj.get("tradeoff_summary") else [],
                    "issues": out_obj.get("issues"),
                    "tradeoffSummary": out_obj.get("tradeoff_summary"),
                    "phases": out_obj.get("phases") or [],
                }

            final_role = _role_payload("final", "claude")
            return {
                "ok": True,
                "data": {
                    "goal": plan_row.get("goal") or str(payload.get("goal") or ""),
                    "planId": target_plan_id,
                    "diffSummary": f"Loaded council session for {target_plan_id}.",
                    "tradeoffSummary": final_role.get("tradeoffSummary") or "",
                    "roles": {
                        "draft": _role_payload("draft", "gemini"),
                        "critique": _role_payload("critique", "codex"),
                        "final": final_role,
                    },
                },
            }

        if method == "council.approve":
            plan_id = str(payload.get("planId") or f"council-{len(self._store.list_plans()) + 1}")
            goal = str(payload.get("goal") or "")
            projects = self._store.list_projects()
            project_id = str(payload.get("projectId") or self._active_project_id or (projects[0]["id"] if projects else "default"))
            title = str(payload.get("title") or (f"Council Plan: {goal[:40]}" if goal else "Council Plan"))
            phases_data = payload.get("phases") or []
            for phase in phases_data:
                existing = self._store.get_phase(str(phase["id"]))
                if existing and existing["plan_id"] != plan_id:
                    return {"ok": False, "error": f"Phase ID {phase['id']} belongs to another plan."}
            if not self._store.get_project(project_id):
                self._store.create_project(project_id, project_id, "owner/repo", ".")
            self._store.create_plan(plan_id, project_id, title, goal=goal)
            approved_ids = {str(p["id"]) for p in phases_data}
            for old_ph in self._store.list_phases(plan_id):
                if str(old_ph["id"]) not in approved_ids:
                    self._store.delete_phase(str(old_ph["id"]))
            for idx, p in enumerate(phases_data):
                pid = str(p["id"])
                deps = p.get("depends_on") or []
                deps_json = json.dumps(deps) if isinstance(deps, list) else str(deps)
                self._store.upsert_phase(
                    phase_id=pid,
                    plan_id=plan_id,
                    title=str(p.get("title", pid)),
                    branch=str(p.get("branch", f"feat/{pid}")),
                    prompt=str(p.get("prompt", "")),
                    depends_on_json=deps_json,
                    size=str(p.get("size", "medium")),
                    position=idx,
                    ladder=str(p.get("ladder", "default")),
                    timeout_minutes=int(p.get("timeout_minutes", 180)),
                    auto_merge=bool(p.get("auto_merge", True)),
                    release_after_merge=bool(p.get("release_after_merge", False)),
                )
                self._upsert_planned_engine(pid, p.get("implementer"))
            self._events.emit(
                Event(
                    kind="council.approved",
                    plain_text=f"Approved council plan '{title}' with {len(phases_data)} phase(s) and sent to Plan Builder.",
                    level="info",
                )
            )
            return {"ok": True, "data": {"approved": True, "planId": plan_id}}

        # Runs history
        if method == "runs.list":
            plans = self._store.list_plans()
            runs = []
            for p in plans:
                phases = self._store.list_phases(p["id"])
                plan_status = str(p.get("status", "draft")).lower()
                all_steps: list[dict[str, Any]] = []
                for ph in phases:
                    all_steps.extend(self._store.list_steps(ph["id"]))
                has_non_pending_phase = any(
                    str(ph.get("status", "pending")).lower() != "pending" for ph in phases
                )
                tasks_for_plan = self._store.list_tasks(plan_id=p["id"])
                if plan_status == "draft" and not all_steps and not has_non_pending_phase and not tasks_for_plan:
                    continue

                done_phases = [ph for ph in phases if ph.get("status") in ("merged", "completed")]
                failed_phases = [ph for ph in phases if ph.get("status") in ("failed", "parked")]
                runners_used = {str(s["runner"]) for s in all_steps if s.get("runner")}
                total_secs = 0.0
                for s in all_steps:
                    st_ts = s.get("started_at")
                    fn_ts = s.get("finished_at")
                    if st_ts and fn_ts:
                        try:
                            dt0 = datetime.fromisoformat(str(st_ts).replace("Z", "+00:00"))
                            dt1 = datetime.fromisoformat(str(fn_ts).replace("Z", "+00:00"))
                            total_secs += max(0.0, (dt1 - dt0).total_seconds())
                        except Exception:
                            pass
                if total_secs > 0:
                    mins = int(round(total_secs / 60.0))
                    duration_str = f"{mins // 60} h {mins % 60} m" if mins >= 60 else f"{max(1, mins)} m"
                else:
                    duration_str = "-"

                runs.append({
                    "id": p["id"],
                    "projectId": p.get("project_id", ""),
                    "title": p.get("title", ""),
                    "status": "completed" if len(done_phases) == len(phases) and phases else (p.get("status", "draft")),
                    "phasesCount": len(phases),
                    "phasesDone": len(done_phases),
                    "phasesFailed": len(failed_phases),
                    "duration": duration_str,
                    "aiUsed": sorted(runners_used),
                    "createdAt": p.get("created_at", ""),
                    "updatedAt": p.get("updated_at", ""),
                })
            return {"ok": True, "data": runs}

        if method == "runs.get":
            run_id = str(payload.get("runId") or "")
            plan = self._store.get_plan(run_id)
            if not plan:
                plans = self._store.list_plans()
                plan = plans[0] if plans else {"id": run_id, "title": "Run", "project_id": "default"}
            phases = self._store.list_phases(plan.get("id"))
            proj = self._store.get_project(plan.get("project_id", ""))
            repo = proj.get("repo", "") if proj else ""
            formatted_phases = []
            for ph in phases:
                steps = self._store.list_steps(ph["id"])
                runner = str(steps[-1]["runner"]) if steps and steps[-1].get("runner") else "-"
                pr_num = ph.get("pr_number")
                ph_secs = 0.0
                for s in steps:
                    if s.get("started_at") and s.get("finished_at"):
                        try:
                            dt0 = datetime.fromisoformat(str(s["started_at"]).replace("Z", "+00:00"))
                            dt1 = datetime.fromisoformat(str(s["finished_at"]).replace("Z", "+00:00"))
                            ph_secs += max(0.0, (dt1 - dt0).total_seconds())
                        except Exception:
                            pass
                ph_dur = f"{max(1, int(round(ph_secs / 60.0)))} m" if ph_secs > 0 else "-"
                formatted_phases.append({
                    "id": ph["id"],
                    "title": ph.get("title", ph["id"]),
                    "status": ph.get("status", "pending"),
                    "runner": runner,
                    "pr_number": pr_num,
                    "pr_url": f"https://github.com/{repo}/pull/{pr_num}" if (pr_num and repo) else None,
                    "branch": ph.get("branch", ""),
                    "duration": ph_dur,
                })
            digest_lines = [
                f"# Morning Digest for {plan.get('title')}",
                "",
                f"**Project**: {proj.get('name', plan.get('project_id', '')) if proj else 'Default'}",
                f"**Repo**: {repo or '-'}",
                "",
                "### Phases",
            ]
            for ph in formatted_phases:
                status_mark = "Merged" if ph["status"] in ("merged", "completed") else ph["status"].capitalize()
                pr_part = f" (PR #{ph['pr_number']})" if ph["pr_number"] else ""
                digest_lines.append(f"- **{ph['title']}**: {status_mark} via {ph['runner']}{pr_part}")
            digest = "\n".join(digest_lines)
            artifacts: list[dict[str, str]] = []
            if repo:
                for rel in self._store.list_release_verifications(repo=repo, limit=5):
                    tag = str(rel.get("tag") or "")
                    if tag:
                        artifacts.append({
                            "name": f"Release {tag}",
                            "size": str(rel.get("smoke_status") or "-"),
                            "sha256": str(rel.get("merge_sha") or ""),
                        })
            return {
                "ok": True,
                "data": {
                    "run": next((run for run in self.call("runs.list")["data"] if run["id"] == plan["id"]), {"id": plan["id"], "title": plan.get("title", ""), "projectId": plan.get("project_id", ""), "status": plan.get("status", "draft"), "phasesCount": len(phases), "phasesDone": 0, "phasesFailed": 0, "duration": "-", "aiUsed": [], "createdAt": plan.get("created_at", ""), "updatedAt": plan.get("updated_at", "")}),
                    "digest": digest,
                    "phases": formatted_phases,
                    "artifacts": artifacts,
                },
            }

        if method == "runs.openArtifactFolder":
            proj_id = str(payload.get("projectId") or self._active_project_id or "")
            proj = self._store.get_project(proj_id) if proj_id else None
            folder = ((proj.get("output_dir") or proj.get("output_folder")) if proj else "") or str(
                self._store.db_path.parent / "artifacts"
            )
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
                if hasattr(os, "startfile"):
                    os.startfile(folder)
            except Exception:
                pass
            return {"ok": True, "data": {"opened": True, "path": folder}}

        # Engines catalogue and quota
        if method == "engines.list":
            engines = self._store.list_engines()
            if not engines:
                engines = [
                    {"id": "agy", "runner": "agy", "model": "auto", "effort": "medium", "state": "ready", "quotaUsedPct": 0, "coolingUntil": None},
                    {"id": "gemini", "runner": "gemini", "model": "gemini-2.5-pro", "effort": "medium", "state": "ready", "quotaUsedPct": 0, "coolingUntil": None},
                    {"id": "codex", "runner": "codex", "model": "o3-mini", "effort": "medium", "state": "ready", "quotaUsedPct": 0, "coolingUntil": None},
                    {"id": "claude", "runner": "claude", "model": "claude-3-7-sonnet", "effort": "medium", "state": "ready", "quotaUsedPct": 0, "coolingUntil": None},
                ]
            names = {
                "agy": "Antigravity",
                "gemini": "Gemini CLI",
                "codex": "Codex CLI",
                "claude": "Claude Code",
                "qwen": "Qwen Code",
                "deepseek": "DeepSeek",
                "kimi": "Kimi K2",
                "glm": "GLM",
                "minimax": "MiniMax",
            }
            formatted = []
            for engine in engines:
                runner = engine["runner"]
                model = engine.get("model", "")
                reset_dt = self._quota.get_reset_at(runner, model)
                cooling_until = reset_dt.strftime("%H:%M") if reset_dt else engine.get("coolingUntil")
                if not engine.get("enabled", 1):
                    state = "unavailable"
                elif cooling_until:
                    state = "cooling"
                else:
                    state = engine.get("state", "ready")
                try:
                    skills = json.loads(engine.get("skills_json", "{}"))
                except Exception:
                    skills = {}
                try:
                    budget = json.loads(engine.get("budget_json", "{}"))
                except Exception:
                    budget = {}
                display_name = names.get(engine["id"], names.get(runner, runner))
                formatted.append({
                    **engine,
                    "name": display_name,
                    "costTier": engine.get("cost_tier", "standard"),
                    "state": state,
                    "coolingUntil": cooling_until,
                    "quotaUsedPct": engine.get("quotaUsedPct", 0),
                    "skills": skills,
                    "budget": budget,
                })
            return {"ok": True, "data": formatted}

        if method == "engines.update":
            eng_id = str(payload.get("id", ""))
            existing = self._store.get_engine(eng_id) or {}
            cmd_tmpl = existing.get("command_template", "[]")
            if "command_template" in payload:
                raw_t = payload["command_template"]
                cmd_tmpl = json.dumps(raw_t) if isinstance(raw_t, list) else str(raw_t)
            qp_json = existing.get("quota_patterns_json", "[]")
            if "quota_patterns" in payload:
                qp_json = json.dumps(list(payload["quota_patterns"]))
            self._store.upsert_engine(
                engine_id=eng_id,
                runner=str(payload.get("runner", existing.get("runner", eng_id))),
                model=str(payload.get("model", existing.get("model", "auto"))),
                effort=str(payload.get("effort", existing.get("effort", "medium"))),
                skills_json=json.dumps(payload["skills"]) if "skills" in payload else existing.get("skills_json", "{}"),
                cost_tier=str(payload.get("costTier", existing.get("cost_tier", "standard"))),
                budget_json=json.dumps(payload["budget"]) if "budget" in payload else existing.get("budget_json", "{}"),
                enabled=int(payload.get("enabled", existing.get("enabled", 1))),
                pinned=int(payload.get("pinned", existing.get("pinned", 0))),
                command_template=cmd_tmpl,
                usage_parser=str(payload.get("usage_parser", existing.get("usage_parser", ""))),
                quota_patterns_json=qp_json,
            )
            self._events.emit(
                Event(
                    kind="engine.updated",
                    plain_text=f"Updated engine {eng_id} configuration; changes apply from the next phase.",
                    level="info",
                )
            )
            return {"ok": True, "data": {"updated": eng_id}}

        if method == "engines.addEngine":
            eng_id = str(payload.get("id") or "").strip().lower()
            runner = str(payload.get("runner") or eng_id).strip()
            model = str(payload.get("model") or "").strip()
            if not eng_id or not runner:
                return {"ok": False, "error": "Engine requires an ID and runner CLI name."}
            raw_tmpl = payload.get("command_template") or [runner, "-p", "{instruction}"]
            if isinstance(raw_tmpl, str):
                try:
                    parsed_tmpl = json.loads(raw_tmpl)
                    if not isinstance(parsed_tmpl, list) or not all(isinstance(x, str) for x in parsed_tmpl):
                        return {"ok": False, "error": "Command template must be a JSON array of strings."}
                    tmpl_json = json.dumps(parsed_tmpl)
                except Exception as exc:
                    return {"ok": False, "error": f"Invalid command template JSON: {exc}"}
            elif isinstance(raw_tmpl, list):
                tmpl_json = json.dumps([str(x) for x in raw_tmpl])
            else:
                return {"ok": False, "error": "Command template must be an argument list."}

            raw_patterns = payload.get("quota_patterns") or []
            patterns_list = [str(p).strip() for p in raw_patterns if str(p).strip()]
            skills = payload.get("skills") or {"backend": 3, "ui": 3, "fix_ci": 3, "docs": 3, "plan": 2, "review": 2}
            budget = payload.get("budget") or {}
            self._store.upsert_engine(
                engine_id=eng_id,
                runner=runner,
                model=model,
                effort=str(payload.get("effort") or "medium"),
                skills_json=json.dumps(skills),
                cost_tier=str(payload.get("costTier") or "economy"),
                budget_json=json.dumps(budget),
                enabled=1,
                pinned=0,
                command_template=tmpl_json,
                usage_parser=str(payload.get("usage_parser") or ""),
                quota_patterns_json=json.dumps(patterns_list),
            )
            if payload.get("baseUrl"):
                self._store.set_setting(f"engine.{eng_id}.base_url", str(payload["baseUrl"]).strip())
            self._events.emit(
                Event(
                    kind="engine.added",
                    plain_text=f"Added AI engine {eng_id} ({runner}) to catalogue; changes apply from the next phase.",
                    level="info",
                )
            )
            return {"ok": True, "data": {"added": eng_id}}

        if method == "engines.saveKey":
            eng_id = str(payload.get("engineId") or payload.get("id") or "").strip().lower()
            key = str(payload.get("key") or "")
            if not eng_id:
                return {"ok": False, "error": "Engine ID is required to save an API key."}
            self._creds.set_secret(f"api_key_{eng_id}", key)
            self._events.emit(
                Event(
                    kind="engine.key_saved",
                    plain_text=f"Saved encrypted API key for {eng_id}; changes apply from the next phase.",
                    level="info",
                )
            )
            return {"ok": True, "data": {"saved": True, "engineId": eng_id}}

        if method == "engines.test":
            runner = str(payload.get("runner") or payload.get("engineId") or "").strip()
            if not runner:
                return {"ok": False, "error": "Specify a CLI runner to test."}
            eng = self._store.get_engine(runner)
            target_runner = eng["runner"] if eng else runner
            ok, message = test_engine_cli_version(target_runner)
            return {
                "ok": True,
                "data": {
                    "ok": ok,
                    "output": message if ok else None,
                    "error": None if ok else message,
                },
            }

        if method == "engines.checkLogins":
            projects = self._store.list_projects()
            active_id = self._active_project_id or (projects[0]["id"] if projects else "")
            proj = self._store.get_project(active_id) if active_id else None
            repo = str(payload.get("repo") or (proj["repo"] if proj else "owner/repo"))
            checks = self._logins.check_all(repo=repo, force=True)
            return {"ok": True, "data": {name: asdict(result) for name, result in checks.items()}}

        if method == "engines.getLadders":
            ladders_raw = self._store.get_setting("engines.ladders")
            raw_ladders = json.loads(ladders_raw) if ladders_raw else {
                "backend": ["agy", "gemini", "codex", "claude"],
                "ui": ["agy", "gemini", "claude"],
                "review": ["gemini", "codex", "claude"],
                "ci_fix": ["codex", "gemini", "claude"],
                "small_job_ladder": ["qwen", "deepseek", "kimi", "glm", "minimax", "gemini", "codex", "agy"],
                "default": ["agy", "gemini", "codex", "claude"],
            }
            normalized_ladders: dict[str, list[dict[str, str | None]]] = {}
            for cat, entries in (raw_ladders.items() if isinstance(raw_ladders, dict) else []):
                norm_list: list[dict[str, str | None]] = []
                for entry in entries if isinstance(entries, list) else []:
                    if isinstance(entry, dict):
                        runner = str(entry.get("runner") or entry.get("id") or "").strip()
                        model = str(entry.get("model")).strip() if entry.get("model") else None
                        effort = str(entry.get("effort")).strip() if entry.get("effort") else None
                        if runner:
                            norm_list.append({"runner": runner, "model": model, "effort": effort})
                    elif isinstance(entry, str) and entry.strip():
                        norm_list.append({"runner": entry.strip(), "model": None, "effort": None})
                normalized_ladders[str(cat)] = norm_list
            return {
                "ok": True,
                "data": {
                    "ladders": normalized_ladders,
                    "pinnedNote": "Claude is pinned last due to token cost; reserved for complex failovers.",
                },
            }

        if method == "engines.setLadders":
            new_ladders = payload.get("ladders") or {}
            sanitized: dict[str, list[Any]] = {}
            for cat, runners in new_ladders.items():
                non_claude: list[Any] = []
                claude_entries: list[Any] = []
                for r in runners:
                    runner_id = str(r.get("runner") or "") if isinstance(r, dict) else str(r)
                    if runner_id == "claude":
                        claude_entries.append(r)
                    else:
                        non_claude.append(r)
                if claude_entries:
                    non_claude.extend(claude_entries)
                else:
                    non_claude.append("claude")
                sanitized[cat] = non_claude
            self._store.set_setting("engines.ladders", json.dumps(sanitized))
            return {
                "ok": True,
                "data": {
                    "ladders": sanitized,
                    "pinnedNote": "Claude is pinned last due to token cost; reserved for complex failovers.",
                },
            }

        if method == "engines.getLedger":
            entries = self._store.get_ledger_entries(limit=50)
            raw_decisions = self._store.list_router_decisions(limit=20)
            decisions = [
                {
                    "id": d["id"],
                    "step": f"Step #{d['step_id']}" if d.get("step_id") else "Router selection",
                    "timestamp": str(d.get("created_at", ""))[11:16] or "",
                    "chosen": d["chosen"],
                    "reason": d["reason"],
                }
                for d in raw_decisions
            ]
            names = {
                "agy": "Antigravity",
                "gemini": "Gemini CLI",
                "codex": "Codex CLI",
                "claude": "Claude Code",
                "qwen": "Qwen Code",
                "deepseek": "DeepSeek",
                "kimi": "Kimi K2",
                "glm": "GLM",
                "minimax": "MiniMax",
            }
            agg: dict[str, dict[str, Any]] = {}
            for entry in entries:
                runner = str(entry.get("runner") or "unknown")
                tokens = int(entry.get("tokens_in") or 0) + int(entry.get("tokens_out") or 0)
                cost = float(entry.get("cost_usd_est") or 0.0)
                bucket = agg.setdefault(runner, {"tokens": 0, "cost": 0.0})
                bucket["tokens"] += tokens
                bucket["cost"] += cost
            for p_entry in self._store.list_pipeline_ledger(limit=200):
                runner = str(p_entry.get("engine_id") or "unknown")
                tokens = int(p_entry.get("tokens_in") or 0) + int(p_entry.get("tokens_out") or 0)
                cost = float(p_entry.get("est_token_cost_usd") or 0.0)
                if runner not in agg:
                    agg[runner] = {"tokens": tokens, "cost": cost}
                elif not entries:
                    agg[runner]["tokens"] += tokens
                    agg[runner]["cost"] += cost

            max_tokens = max((v["tokens"] for v in agg.values()), default=0)
            chart_data = [
                {
                    "runner": runner,
                    "name": names.get(runner, runner),
                    "tokens": int(stats["tokens"]),
                    "costEstimate": f"${stats['cost']:.2f} (Est.)" if stats["cost"] > 0 else "$0.00 (Flat)",
                    "pct": int(round((stats["tokens"] / max_tokens) * 100)) if max_tokens > 0 else 0,
                }
                for runner, stats in agg.items()
            ]
            return {
                "ok": True,
                "data": {
                    "entries": entries,
                    "routerDecisions": decisions,
                    "chartData": chart_data,
                    "isEstimate": True,
                },
            }

        # Settings: projects CRUD, priority list, guard limits, data folder, keep awake
        if method == "settings.getProjects":
            return {"ok": True, "data": self._store.list_projects()}

        if method == "settings.saveProject":
            p_data = payload.get("project") or payload
            pid = str(p_data.get("id") or "").strip()
            name = str(p_data.get("name") or pid).strip()
            repo = str(p_data.get("repo") or "").strip()
            local_path = str(p_data.get("local_path") or p_data.get("local_repo") or "").strip()
            build_rules = str(p_data.get("build_rules") or "")
            artifact_glob = str(p_data.get("artifact_glob") or "*.apk")
            output_dir = str(p_data.get("output_dir") or p_data.get("output_folder") or "")

            if not pid or not repo:
                return {"ok": False, "error": "Project requires an ID and GitHub repository (owner/repo)."}

            try:
                evidence_kwargs = self._extract_project_evidence_kwargs(p_data)
            except (ValueError, TypeError) as exc:
                return {"ok": False, "error": f"Invalid evidence configuration: {exc}"}

            existing = self._store.get_project(pid)
            if existing:
                updated = self._store.update_project(
                    pid,
                    name=name,
                    repo=repo,
                    local_path=local_path,
                    build_rules=build_rules,
                    artifact_glob=artifact_glob,
                    output_dir=output_dir,
                    **evidence_kwargs,
                )
                return {"ok": True, "data": updated}
            else:
                created = self._store.create_project(
                    pid,
                    name,
                    repo,
                    local_path,
                    build_rules=build_rules,
                    artifact_glob=artifact_glob,
                    output_dir=output_dir,
                    **evidence_kwargs,
                )
                self._store.set_setting("onboarding.completed", "1")
                return {"ok": True, "data": created}

        if method == "settings.deleteProject":
            pid = str(payload.get("id") or "")
            if not pid:
                return {"ok": False, "error": "Project ID required."}
            self._store.delete_project(pid)
            if self._active_project_id == pid:
                remaining = self._store.list_projects()
                self._active_project_id = remaining[0]["id"] if remaining else None
            return {"ok": True, "data": {"deleted": pid}}

        if method == "settings.getPriorityList":
            p_list = self._store.get_priority_list()
            if not p_list:
                p_list = [
                    {"position": 1, "engine": "agy", "model": "auto", "effort": "medium"},
                    {"position": 2, "engine": "gemini", "model": "gemini-2.5-pro", "effort": "medium"},
                    {"position": 3, "engine": "codex", "model": "o3-mini", "effort": "medium"},
                    {"position": 4, "engine": "claude", "model": "claude-3-7-sonnet", "effort": "medium"},
                ]
            latest_logins = self._store.get_latest_login_checks()
            enriched = []
            for item in p_list:
                eng = item["engine"]
                reset_dt = self._quota.get_reset_at(eng, item.get("model", ""))
                login = latest_logins.get(eng)
                if reset_dt:
                    availability = "Cooling down"
                    back_at = reset_dt.strftime("%H:%M")
                elif login and login.get("state") != "Ready":
                    availability = str(login["state"])
                    back_at = login.get("back_at")
                else:
                    availability = "Ready"
                    back_at = None
                enriched.append({**item, "availability": availability, "backAt": back_at})
            return {"ok": True, "data": enriched}

        if method == "settings.setPriorityList":
            items = payload.get("items") or []
            self._store.set_priority_list(items)
            order_names = ", ".join(str(i.get("engine", "")) for i in items)
            self._events.emit(
                Event(
                    kind="settings.priority_updated",
                    plain_text=f"Updated global fallback priority order ({order_names}); changes apply from the next phase.",
                    level="info",
                )
            )
            return {"ok": True, "data": items}

        if method == "settings.getGuardLimits":
            return {
                "ok": True,
                "data": {
                    "maxAttempts": int(self._store.get_setting("guard.maxAttempts", "3") or "3"),
                    "maxCiRetries": int(self._store.get_setting("guard.maxCiRetries", "4") or "4"),
                    "maxRunInvocations": int(self._store.get_setting("guard.maxRunInvocations", "30") or "30"),
                    "runnerTimeoutMinutes": int(self._store.get_setting("guard.runnerTimeoutMinutes", "90") or "90"),
                },
            }

        if method == "settings.setGuardLimits":
            if "maxAttempts" in payload:
                self._store.set_setting("guard.maxAttempts", str(payload["maxAttempts"]))
            if "maxCiRetries" in payload:
                self._store.set_setting("guard.maxCiRetries", str(payload["maxCiRetries"]))
            if "maxRunInvocations" in payload:
                self._store.set_setting("guard.maxRunInvocations", str(payload["maxRunInvocations"]))
            if "runnerTimeoutMinutes" in payload:
                self._store.set_setting("guard.runnerTimeoutMinutes", str(payload["runnerTimeoutMinutes"]))
            return {"ok": True, "data": payload}

        if method == "settings.getDataFolder":
            folder = str(self._store.db_path.parent)
            return {"ok": True, "data": {"path": folder}}

        if method == "settings.openDataFolder":
            folder = str(self._store.db_path.parent)
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
                if hasattr(os, "startfile"):
                    os.startfile(folder)
            except Exception:
                pass
            return {"ok": True, "data": {"opened": True, "path": folder}}

        if method == "settings.setKeepAwake":
            enabled = bool(payload.get("enabled", True))
            set_keep_awake(enabled)
            self._store.set_setting("system.keepAwake", "1" if enabled else "0")
            return {"ok": True, "data": {"keepAwake": enabled}}

        if method == "settings.getAutostart":
            enabled = (
                self._autostart.is_enabled()
                if self._autostart.supported
                else self._store.get_setting("system.autostart") == "1"
            )
            return {
                "ok": True,
                "data": {
                    "enabled": enabled,
                    "supported": self._autostart.supported,
                    "method": "task-scheduler",
                },
            }

        if method == "settings.setAutostart":
            enabled = bool(payload.get("enabled", False))
            ok, detail = self._autostart.set_enabled(enabled)
            if not ok:
                return {"ok": False, "error": detail}
            self._store.set_setting("system.autostart", "1" if enabled else "0")
            self._events.emit(
                Event(
                    kind="settings.autostart_updated",
                    plain_text=detail,
                    level="info",
                )
            )
            return {
                "ok": True,
                "data": {
                    "enabled": enabled,
                    "supported": self._autostart.supported,
                    "method": "task-scheduler",
                    "detail": detail,
                },
            }

        # Full app snapshot for desktop UI state
        if method in ("app.snapshot", "app.selectProject"):
            projects = self._store.list_projects()
            active_project_id = str(payload.get("projectId") or "")
            if not active_project_id and projects:
                valid_active = (
                    self._active_project_id
                    if self._active_project_id and any(p["id"] == self._active_project_id for p in projects)
                    else None
                )
                active_project_id = valid_active or projects[0]["id"]
            self._active_project_id = active_project_id or None
            active_project = next((p for p in projects if p["id"] == active_project_id), None)
            checks = self._logins.check_all(repo=active_project["repo"], force=False) if active_project else {}

            deck = generate_deck_data(self._store, project_id=active_project_id or "nightshift-desktop")
            is_running = bool(self._engine and getattr(self._engine, "is_running", False))
            stop_armed = bool(getattr(self._engine, "stop_requested", self._stop_armed))
            parked = self._store.list_parked(project_id=active_project_id) if active_project_id else []

            run_state = "running" if is_running else ("failed" if deck.get("needs_you") else "idle")
            scenario = "empty" if not projects else ("running" if is_running else ("failure" if parked else "idle"))

            project_plans = self._store.list_plans(active_project_id) if active_project_id else []
            selected_plan = project_plans[0] if project_plans else None
            phases = self._store.list_phases(selected_plan["id"]) if selected_plan else []
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
                    "logins": {name: asdict(result) for name, result in checks.items()},
                },
            }

        # Pipeline governance controls (Phase 8e: owner approval, assertion waiver, release rollback)
        if method == "pipeline.listTasks":
            status_filter = str(payload.get("status") or "").strip() or None
            plan_filter = str(payload.get("planId") or "").strip() or None
            project_filter = str(payload.get("projectId") or "").strip() or None
            if project_filter:
                if self._store.get_project(project_filter) is None:
                    return {"ok": False, "error": f"Unknown project: {project_filter}"}
                if plan_filter:
                    plan_row = self._store.get_plan(plan_filter)
                    if plan_row is None or str(plan_row.get("project_id") or "") != project_filter:
                        return {
                            "ok": False,
                            "error": f"Plan '{plan_filter}' does not belong to project '{project_filter}'.",
                        }
            tasks = self._store.list_tasks(
                status=status_filter,
                plan_id=plan_filter,
                project_id=project_filter,
            )
            return {"ok": True, "data": tasks}

        if method == "pipeline.approveTask":
            task_id = str(payload.get("taskId") or "").strip()
            head_sha = str(payload.get("headSha") or "").strip()
            approved_by = str(payload.get("approvedBy") or "owner").strip() or "owner"
            resume = bool(payload.get("resume", True))
            req_project_id = str(payload.get("projectId") or "").strip()
            req_repo = str(payload.get("repo") or "").strip()
            if not task_id or not head_sha:
                return {"ok": False, "error": "taskId and exact headSha are required to approve a task."}
            task = self._store.get_task(task_id)
            if task is None:
                return {"ok": False, "error": f"Task not found: {task_id}"}
            task_project_id = self._store.get_task_project_id(task_id)
            if not task_project_id:
                return {"ok": False, "error": f"Task '{task_id}' has no owning project."}
            expected_project_id = req_project_id or (self._active_project_id or "")
            if expected_project_id and task_project_id != expected_project_id:
                return {
                    "ok": False,
                    "error": f"Task '{task_id}' does not belong to project '{expected_project_id}'.",
                }
            if req_repo:
                owning_proj = self._store.get_project(task_project_id)
                owning_repo = str(owning_proj.get("repo") or "").strip() if owning_proj else ""
                if owning_repo != req_repo:
                    return {
                        "ok": False,
                        "error": f"Task '{task_id}' does not belong to repository '{req_repo}'.",
                    }
            if task.get("head_sha") and str(task["head_sha"]) != head_sha:
                return {
                    "ok": False,
                    "error": f"Head SHA mismatch: task is at {str(task['head_sha'])[:7]}, not {head_sha[:7]}.",
                }
            updated = self._store.record_task_owner_approval(task_id, approved_by, head_sha)
            if resume and updated.get("status") == "BLOCKED":
                from nightshift.engine.pipeline_orchestrator import InvalidTransitionError, TaskOrchestrator

                try:
                    updated = TaskOrchestrator(self._store).resume_from_blocked(
                        task_id,
                        current_head_sha=head_sha,
                        owner_approved_by=approved_by,
                        actor=approved_by,
                    )
                except InvalidTransitionError as exc:
                    return {"ok": False, "error": str(exc)}
                if updated.get("status") != "BLOCKED" and updated.get("phase_id"):
                    self._store.unpark_phase(str(updated["phase_id"]))
            self._events.emit(
                Event(
                    kind="pipeline.owner_approved",
                    plain_text=f"Owner approved task {task_id} for commit {head_sha[:7]}.",
                    level="info",
                    phase_id=str(updated.get("phase_id") or "") or None,
                )
            )
            return {"ok": True, "data": updated}

        if method == "pipeline.recordAssertionWaiver":
            task_id = str(payload.get("taskId") or "").strip()
            head_sha = str(payload.get("headSha") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            req_project_id = str(payload.get("projectId") or "").strip()
            req_repo = str(payload.get("repo") or "").strip()
            if not task_id or not head_sha or not reason:
                return {"ok": False, "error": "taskId, exact headSha, and written rationale are required."}
            task = self._store.get_task(task_id)
            if task is None:
                return {"ok": False, "error": f"Task not found: {task_id}"}
            task_project_id = self._store.get_task_project_id(task_id)
            if not task_project_id:
                return {"ok": False, "error": f"Task '{task_id}' has no owning project."}
            expected_project_id = req_project_id or (self._active_project_id or "")
            if expected_project_id and task_project_id != expected_project_id:
                return {
                    "ok": False,
                    "error": f"Task '{task_id}' does not belong to project '{expected_project_id}'.",
                }
            if req_repo:
                owning_proj = self._store.get_project(task_project_id)
                owning_repo = str(owning_proj.get("repo") or "").strip() if owning_proj else ""
                if owning_repo != req_repo:
                    return {
                        "ok": False,
                        "error": f"Task '{task_id}' does not belong to repository '{req_repo}'.",
                    }
            try:
                updated = self._store.record_task_assertion_waiver(task_id, head_sha, reason)
            except KeyError as exc:
                return {"ok": False, "error": str(exc)}
            self._events.emit(
                Event(
                    kind="pipeline.assertion_waiver",
                    plain_text=f"Recorded owner assertion waiver for {task_id} at {head_sha[:7]}.",
                    level="warn",
                    phase_id=str(updated.get("phase_id") or "") or None,
                )
            )
            return {"ok": True, "data": updated}

        if method == "pipeline.listReleaseVerifications":
            repo_filter = str(payload.get("repo") or "").strip() or None
            project_filter = str(payload.get("projectId") or "").strip() or None
            limit = int(payload.get("limit") or 20)
            if project_filter:
                proj = self._store.get_project(project_filter)
                if proj is None:
                    return {"ok": False, "error": f"Unknown project: {project_filter}"}
                proj_repo = str(proj.get("repo") or "").strip()
                if repo_filter and repo_filter != proj_repo:
                    return {
                        "ok": False,
                        "error": f"Repository '{repo_filter}' does not belong to project '{project_filter}'.",
                    }
            rows = self._store.list_release_verifications(
                repo=repo_filter,
                project_id=project_filter,
                limit=limit,
            )
            return {"ok": True, "data": rows}

        if method == "pipeline.rollbackRelease":
            from nightshift.engine.release import ReleaseManager
            from nightshift.engine.vcs import SubprocessGitGh

            faulty_tag = str(payload.get("tag") or payload.get("faultyTag") or "").strip()
            if not faulty_tag:
                return {"ok": False, "error": "Release tag is required for rollback."}
            req_project_id = str(payload.get("projectId") or "").strip()
            req_repo = str(payload.get("repo") or "").strip()
            project_id = req_project_id or (self._active_project_id or "")
            proj: dict[str, Any] | None = None
            if project_id:
                proj = self._store.get_project(project_id)
                if proj is None:
                    return {"ok": False, "error": f"Unknown project: {project_id}"}
                proj_repo = str(proj.get("repo") or "").strip()
                if not proj_repo:
                    return {"ok": False, "error": f"Project '{project_id}' has no configured repository."}
                if req_repo and req_repo != proj_repo:
                    return {
                        "ok": False,
                        "error": f"Repository '{req_repo}' does not belong to project '{project_id}'.",
                    }
                repo = proj_repo
            else:
                repo = req_repo
            if not repo:
                return {"ok": False, "error": "Repository (repo or projectId) is required for rollback."}
            rel_rec = self._store.get_release_verification(repo, faulty_tag)
            if rel_rec is None:
                return {"ok": False, "error": f"Unknown release tag '{faulty_tag}' for repository '{repo}'."}
            if project_id and rel_rec.get("task_id"):
                rel_task_proj = self._store.get_task_project_id(str(rel_rec["task_id"]))
                if rel_task_proj is not None and rel_task_proj != project_id:
                    return {
                        "ok": False,
                        "error": f"Release '{faulty_tag}' belongs to project '{rel_task_proj}', not '{project_id}'.",
                    }
            if proj is None and repo:
                proj = next((p for p in self._store.list_projects() if p.get("repo") == repo), None)
            out_dir = Path(
                str(payload.get("outputDir") or (proj.get("output_dir") if proj else "") or "dist")
            )
            local_repo = Path(str(proj["local_path"])) if proj and proj.get("local_path") else None
            open_revert_pr = bool(payload.get("openRevertPr", False))

            vcs = getattr(self._engine, "vcs", None) or SubprocessGitGh()
            rel_mgr = getattr(self._engine, "release_mgr", None) or ReleaseManager(vcs)
            res = rel_mgr.rollback_release(
                store=self._store,
                repo=repo,
                faulty_tag=faulty_tag,
                output_dir=out_dir,
                open_revert_pr=open_revert_pr,
                local_repo=local_repo,
            )
            if not res.ok:
                return {"ok": False, "error": res.detail}
            self._events.emit(
                Event(
                    kind="release.rolled_back",
                    plain_text=res.detail,
                    level="warn",
                )
            )
            return {"ok": True, "data": asdict(res)}

        handler = self._registry.resolve(method)
        if handler is None:
            return {"ok": False, "error": f"Unknown bridge method: {method}"}
        return {"ok": True, "data": handler(payload)}
