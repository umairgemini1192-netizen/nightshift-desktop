"""Typed, versioned JS API exposed to the React frontend via pywebview."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from nightshift import __version__
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
from nightshift.engine.state_machine import ProjectConfig, set_keep_awake
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
        self._active_project_id: str | None = None
        self._run_lock = threading.Lock()
        self._run_thread: threading.Thread | None = None

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
                plans = self._store.list_plans()
                plan_id = plans[0]["id"] if plans else ""
            plan = self._store.get_plan(plan_id) if plan_id else None
            phases = self._store.list_phases(plan_id) if plan_id else self._store.list_phases()
            return {"ok": True, "data": {"plan": plan, "phases": phases}}

        if method == "plan.save":
            plan_data = payload.get("plan") or {}
            plan_id = str(plan_data.get("id") or "default_plan")
            project_id = str(plan_data.get("project_id") or self._active_project_id or "default")
            title = str(plan_data.get("title") or "Plan")
            goal = str(plan_data.get("goal") or "")
            self._store.create_plan(plan_id, project_id, title, goal)
            phases_data = payload.get("phases") or []
            for idx, p in enumerate(phases_data):
                raw_deps = p.get("depends_on") or []
                deps_json = json.dumps(raw_deps) if isinstance(raw_deps, list) else str(raw_deps)
                self._store.upsert_phase(
                    phase_id=str(p["id"]),
                    plan_id=plan_id,
                    title=str(p.get("title", p["id"])),
                    branch=str(p.get("branch", f"feat/{p['id']}")),
                    prompt=str(p.get("prompt", "")),
                    depends_on_json=deps_json,
                    size=str(p.get("size", "medium")),
                    position=int(p.get("position", idx)),
                    status=str(p.get("status", "pending")),
                    ladder=str(p.get("ladder", "default")), timeout_minutes=int(p.get("timeout_minutes", 180)),
                    auto_merge=bool(p.get("auto_merge", True)), release_after_merge=bool(p.get("release_after_merge", False)),
                    force_rerun=bool(p.get("force_rerun", False)),
                )
            return {"ok": True, "data": {"saved": True, "planId": plan_id}}

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
            problems = validate_plan(phase_defs)
            return {"ok": True, "data": {"valid": len(problems) == 0, "problems": problems}}

        if method == "plan.importQueue":
            queue_payload = payload.get("queue")
            if not queue_payload:
                return {"ok": False, "error": "No queue data provided to import."}
            try:
                imported = import_queue_json(queue_payload)
                proj = imported["project"]
                if not self._store.get_project(proj["id"]):
                    self._store.create_project(
                        proj["id"],
                        proj["name"],
                        proj["repo"],
                        proj["local_path"],
                        build_rules=proj.get("build_rules", ""),
                        output_dir=proj.get("output_dir", ""),
                    )
                plan = imported["plan"]
                self._store.create_plan(plan["id"], proj["id"], plan["title"], plan.get("goal", ""))
                for idx, ph in enumerate(imported["phases"]):
                    self._store.upsert_phase(
                        phase_id=ph.id,
                        plan_id=plan["id"],
                        title=ph.title,
                        branch=ph.branch,
                        prompt=ph.prompt,
                        depends_on_json=json.dumps(ph.depends_on),
                        size=ph.size,
                        position=idx, ladder=ph.ladder, timeout_minutes=ph.timeout_minutes,
                        auto_merge=ph.auto_merge, release_after_merge=ph.release_after_merge, force_rerun=ph.force_rerun,
                    )
                if imported.get("ladders"):
                    self._store.set_setting("engines.ladders", json.dumps(imported["ladders"]))
                return {
                    "ok": True,
                    "data": {
                        "project": proj,
                        "plan": plan,
                        "phases": [ph.to_dict() for ph in imported["phases"]],
                        "ladders": imported["ladders"],
                    },
                }
            except Exception as e:
                return {"ok": False, "error": f"Failed to import queue.json: {e}"}

        if method == "plan.exportQueue":
            plan_id = str(payload.get("planId") or "")
            plans = self._store.list_plans()
            plan = self._store.get_plan(plan_id) if plan_id else (plans[0] if plans else {"id": "default", "title": "Exported Plan", "project_id": "default"})
            project = self._store.get_project(plan.get("project_id", "")) or {"repo": "owner/repo", "local_path": "", "output_dir": ""}
            phases = self._store.list_phases(plan.get("id"))
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
            engine = {"Antigravity": "agy", "Gemini CLI": "gemini", "Codex CLI": "codex", "Claude Code": "claude"}.get(engine, engine)
            model = str(payload.get("model") or "")
            effort = str(payload.get("effort") or "medium")
            reset = bool(payload.get("reset", False))
            for pid in phase_ids:
                if reset:
                    self._store.upsert_phase_assignment(pid, planned_engine="agy", override_engine=None, model=None, effort="medium")
                else:
                    self._store.upsert_phase_assignment(pid, override_engine=engine, model=model, effort=effort)
            return {"ok": True, "data": {"updated": len(phase_ids)}}

        # Runs history
        if method == "runs.list":
            plans = self._store.list_plans()
            runs = []
            for p in plans:
                phases = self._store.list_phases(p["id"])
                done_phases = [ph for ph in phases if ph.get("status") in ("merged", "completed")]
                failed_phases = [ph for ph in phases if ph.get("status") in ("failed", "parked")]
                runners_used = set()
                for ph in phases:
                    steps = self._store.list_steps(ph["id"])
                    for s in steps:
                        if s.get("runner"):
                            runners_used.add(s["runner"])
                runs.append({
                    "id": p["id"],
                    "projectId": p.get("project_id", ""),
                    "title": p.get("title", ""),
                    "status": "completed" if len(done_phases) == len(phases) and phases else (p.get("status", "draft")),
                    "phasesCount": len(phases),
                    "phasesDone": len(done_phases),
                    "phasesFailed": len(failed_phases),
                    "duration": "1 h 15 m",
                    "aiUsed": sorted(list(runners_used)) if runners_used else ["agy", "gemini"],
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
            repo = proj.get("repo", "owner/repo") if proj else "owner/repo"
            formatted_phases = []
            for ph in phases:
                steps = self._store.list_steps(ph["id"])
                runner = steps[-1]["runner"] if steps else "agy"
                pr_num = ph.get("pr_number")
                formatted_phases.append({
                    "id": ph["id"],
                    "title": ph.get("title", ph["id"]),
                    "status": ph.get("status", "pending"),
                    "runner": runner,
                    "pr_number": pr_num,
                    "pr_url": f"https://github.com/{repo}/pull/{pr_num}" if pr_num else None,
                    "branch": ph.get("branch", ""),
                    "duration": "24 m",
                })
            digest_lines = [
                f"# Morning Digest for {plan.get('title')}",
                "",
                f"**Project**: {proj.get('name', 'DriveLift') if proj else 'Default'}",
                f"**Repo**: {repo}",
                "",
                "### Completed Phases",
            ]
            for ph in formatted_phases:
                status_mark = "Merged" if ph["status"] in ("merged", "completed") else ph["status"].capitalize()
                pr_part = f" (PR #{ph['pr_number']})" if ph["pr_number"] else ""
                digest_lines.append(f"- **{ph['title']}**: {status_mark} via {ph['runner']}{pr_part}")
            digest = "\n".join(digest_lines)
            return {
                "ok": True,
                "data": {
                    "run": plan,
                    "digest": digest,
                    "phases": formatted_phases,
                    "artifacts": [
                        {"name": "app-release-unsigned.apk", "size": "18.4 MB", "sha256": "3a8c1f38e07c8d9"},
                        {"name": "SHA256SUMS", "size": "120 B", "sha256": "c8e2b109af4e1d"},
                    ],
                },
            }

        if method == "runs.openArtifactFolder":
            proj_id = str(payload.get("projectId") or self._active_project_id or "")
            proj = self._store.get_project(proj_id) if proj_id else None
            folder = (proj.get("output_dir") or proj.get("output_folder")) if proj else str(Path.home() / "NightShiftArtifacts")
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
                    {"id": "agy", "runner": "agy", "model": "auto", "effort": "medium", "state": "ready", "quotaUsedPct": 65, "coolingUntil": None},
                    {"id": "gemini", "runner": "gemini", "model": "gemini-2.5-pro", "effort": "medium", "state": "ready", "quotaUsedPct": 30, "coolingUntil": None},
                    {"id": "codex", "runner": "codex", "model": "o3-mini", "effort": "medium", "state": "ready", "quotaUsedPct": 20, "coolingUntil": None},
                    {"id": "claude", "runner": "claude", "model": "claude-3-7-sonnet", "effort": "medium", "state": "ready", "quotaUsedPct": 10, "coolingUntil": None},
                ]
            return {"ok": True, "data": engines}

        if method == "engines.update":
            eng_id = str(payload.get("id", ""))
            runner = str(payload.get("runner") or eng_id)
            model = str(payload.get("model") or "auto")
            effort = str(payload.get("effort") or "medium")
            cost_tier = str(payload.get("costTier") or "standard")
            budget = payload.get("budget") or {}
            self._store.upsert_engine(
                engine_id=eng_id,
                runner=runner,
                model=model,
                effort=effort,
                cost_tier=cost_tier,
                budget_json=json.dumps(budget),
            )
            return {"ok": True, "data": {"updated": eng_id}}

        if method == "engines.getLadders":
            ladders_raw = self._store.get_setting("engines.ladders")
            ladders = json.loads(ladders_raw) if ladders_raw else {
                "backend": ["agy", "gemini", "codex", "claude"],
                "ui": ["agy", "gemini", "claude"],
                "review": ["gemini", "codex", "claude"],
                "ci_fix": ["codex", "gemini", "claude"],
                "default": ["agy", "gemini", "codex", "claude"],
            }
            return {
                "ok": True,
                "data": {
                    "ladders": ladders,
                    "pinnedNote": "Claude is pinned last due to token cost; reserved for complex failovers.",
                },
            }

        if method == "engines.setLadders":
            new_ladders = payload.get("ladders") or {}
            sanitized: dict[str, list[str]] = {}
            for cat, runners in new_ladders.items():
                r_list = [r for r in runners if r != "claude"]
                r_list.append("claude")
                sanitized[cat] = r_list
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
            decisions = self._store.list_router_decisions(limit=20)
            chart_data = [
                {"runner": "agy", "name": "Antigravity", "tokens": 42000, "costEstimate": "$0.00 (Flat)", "pct": 65},
                {"runner": "gemini", "name": "Gemini CLI", "tokens": 18500, "costEstimate": "$0.04 (Est.)", "pct": 30},
                {"runner": "codex", "name": "Codex CLI", "tokens": 12000, "costEstimate": "$0.12 (Est.)", "pct": 20},
                {"runner": "claude", "name": "Claude Code", "tokens": 4500, "costEstimate": "$0.09 (Est.)", "pct": 10},
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
                )
                return {"ok": True, "data": created}

        if method == "settings.deleteProject":
            pid = str(payload.get("id") or "")
            if not pid:
                return {"ok": False, "error": "Project ID required."}
            self._store.delete_project(pid)
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
            return {"ok": True, "data": p_list}

        if method == "settings.setPriorityList":
            items = payload.get("items") or []
            self._store.set_priority_list(items)
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

        # Full app snapshot for desktop UI state
        if method in ("app.snapshot", "app.selectProject"):
            projects = self._store.list_projects()
            active_project_id = str(payload.get("projectId") or "")
            if not active_project_id and projects:
                active_project_id = self._active_project_id or projects[0]["id"]
            self._active_project_id = active_project_id or None
            active_project = next((p for p in projects if p["id"] == active_project_id), None)
            checks = self._logins.check_all(repo=active_project["repo"], force=False) if active_project else {}

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
                    "logins": {name: asdict(result) for name, result in checks.items()},
                },
            }

        handler = self._registry.resolve(method)
        if handler is None:
            return {"ok": False, "error": f"Unknown bridge method: {method}"}
        return {"ok": True, "data": handler(payload)}
