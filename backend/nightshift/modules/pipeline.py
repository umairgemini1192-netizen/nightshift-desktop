"""Role-Based Agent Pipeline & Ledger backend module (BLUEPRINT §4, AGENT_PIPELINE §11)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nightshift.engine.pipeline_orchestrator import TaskOrchestrator
from nightshift.engine.pipeline_review import ReleaseGovernance
from nightshift.engine.pipeline_router import (
    PIPELINE_STAGE_MATURITY,
    BudgetGuard,
    BudgetLimits,
    summarize_ci_failure_triage,
)
from nightshift.events import Event, EventBus
from nightshift.modules.base import ModuleBase
from nightshift.store import Store


class PipelineService:
    """Provides typed state queries and owner actions for the Agent Pipeline UI."""

    def __init__(self, store: Store | None = None, events: EventBus | None = None) -> None:
        self._store = store
        self._events = events

    def attach(self, store: Store | None, events: EventBus | None = None) -> None:
        self._store = store
        self._events = events

    def get_state(self) -> dict[str, Any]:
        if self._store is None:
            return {
                "summary": {
                    "acceptedChanges30d": 0,
                    "costPerAcceptedUsdEst": 0.0,
                    "regressionRate": 0.0,
                    "goldSavedCount": 0,
                    "freeOnlyMode": True,
                    "monthlySpendUsdEst": 0.0,
                    "monthlyCapUsdEst": 0.0,
                    "tonightSpendUsdEst": 0.0,
                    "tonightCapUsdEst": 0.0,
                    "isEstimate": True,
                },
                "activeTask": None,
                "tasks": [],
                "modelScores": [],
                "ledger": [],
                "stageMaturity": PIPELINE_STAGE_MATURITY,
                "releases": [],
            }

        tasks = self._store.list_tasks()
        scores = self._store.list_model_scores()
        ledger = self._store.list_pipeline_ledger(limit=200)
        releases = self._store.list_release_verifications(limit=25)

        free_only = (self._store.get_setting("pipeline.free_only_mode", "true") or "true").lower() == "true"
        monthly_cap = float(self._store.get_setting("pipeline.max_month_usd_est", "0.0") or "0.0")
        tonight_cap = float(self._store.get_setting("pipeline.max_night_usd_est", "0.0") or "0.0")
        bguard = BudgetGuard(self._store, BudgetLimits(free_only_mode=free_only, max_month_usd_est=monthly_cap, max_night_usd_est=tonight_cap))

        accepted_tasks = [t for t in tasks if t.get("status") in ("MERGED", "RELEASED")]
        total_accepted = len(accepted_tasks)
        total_cost = sum(float(r.get("est_token_cost_usd", 0.0)) for r in ledger)
        cost_per_accepted = round(total_cost / total_accepted, 4) if total_accepted else 0.0
        reverted = sum(1 for r in ledger if r.get("outcome") == "reverted")
        regression_rate = round((reverted / total_accepted) * 100.0, 1) if total_accepted else 0.0
        gold_saved = sum(
            1
            for r in ledger
            if r.get("outcome") == "accepted" and str(r.get("engine_id", "")).lower() in ("qwen", "deepseek", "kimi", "glm", "minimax")
        )

        enriched_tasks: list[dict[str, Any]] = []
        for t in tasks:
            tid = str(t["id"])
            ev = self._store.list_evidence_records(tid)
            attempts = self._store.list_task_attempts(tid)
            enriched_tasks.append(
                {
                    **t,
                    "contributingProviders": json.loads(str(t.get("contributing_providers_json") or "[]")),
                    "evidence": ev,
                    "attempts": attempts,
                }
            )

        active_task = next(
            (t for t in enriched_tasks if t.get("status") in ("RUNNING", "VERIFYING", "REVIEWING", "BLOCKED", "APPROVED")),
            enriched_tasks[0] if enriched_tasks else None,
        )

        return {
            "summary": {
                "acceptedChanges30d": total_accepted,
                "costPerAcceptedUsdEst": cost_per_accepted,
                "regressionRate": regression_rate,
                "goldSavedCount": gold_saved,
                "freeOnlyMode": free_only,
                "monthlySpendUsdEst": bguard.monthly_spend_usd(),
                "monthlyCapUsdEst": monthly_cap,
                "tonightSpendUsdEst": round(total_cost, 4),
                "tonightCapUsdEst": tonight_cap,
                "isEstimate": True,
            },
            "activeTask": active_task,
            "tasks": enriched_tasks,
            "modelScores": scores,
            "ledger": ledger,
            "stageMaturity": PIPELINE_STAGE_MATURITY,
            "releases": releases,
        }

    def approve_task(self, task_id: str, *, owner: str = "owner") -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("Store is not attached")
        orch = TaskOrchestrator(self._store)
        task = self._store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        head_sha = str(task.get("head_sha") or "")
        if task["status"] == "BLOCKED":
            updated = orch.resume_from_blocked(task_id, current_head_sha=head_sha or None, owner_approved_by=owner, actor=owner)
        else:
            updated = self._store.update_task_fields(
                task_id,
                owner_approved_by=owner,
                owner_approved_sha=head_sha,
                owner_approved_at=datetime.now(timezone.utc).isoformat(),
            )
        if self._events is not None:
            self._events.emit(
                Event(
                    kind="pipeline.task_approved",
                    plain_text=f"Owner approved task {task_id} for commit {head_sha[:7]}.",
                    phase_id=str(task.get("phase_id") or ""),
                )
            )
        return updated

    def waive_assertions(self, task_id: str, *, head_sha: str, reason: str) -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("Store is not attached")
        if not reason.strip():
            raise ValueError("A written rationale is required for an assertion waiver.")
        return self._store.update_task_fields(
            task_id,
            assertion_waiver_sha=head_sha,
            assertion_waiver_reason=reason.strip(),
        )

    def rollback_release(self, tag: str, *, archive_dir: str, output_dir: str) -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("Store is not attached")
        gov = ReleaseGovernance(self._store)
        res = gov.rollback_release(faulty_tag=tag, archive_dir=Path(archive_dir), output_dir=Path(output_dir))
        if self._events is not None:
            self._events.emit(
                Event(
                    kind="pipeline.release_rolled_back",
                    plain_text=f"Rolled back release {tag} to {res['restoredTag']}.",
                )
            )
        return res


class Module(ModuleBase):
    """Auto-discovered backend module exposing ``pipeline.*`` RPC methods."""

    id = "pipeline"

    def __init__(self) -> None:
        self._service = PipelineService()

    def configure(
        self,
        *,
        store: Any = None,
        events: Any = None,
        creds: Any = None,
        engine: Any = None,
    ) -> None:
        self._service.attach(store=store, events=events)

    def api(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "getState": lambda _args: self._service.get_state(),
            "approveTask": lambda args: self._service.approve_task(
                str(args.get("taskId", "")),
                owner=str(args.get("owner", "owner")),
            ),
            "waiveAssertions": lambda args: self._service.waive_assertions(
                str(args.get("taskId", "")),
                head_sha=str(args.get("headSha", "")),
                reason=str(args.get("reason", "")),
            ),
            "rollbackRelease": lambda args: self._service.rollback_release(
                str(args.get("tag", "")),
                archive_dir=str(args.get("archiveDir", "")),
                output_dir=str(args.get("outputDir", "")),
            ),
            "stageMaturity": lambda _args: {"stages": PIPELINE_STAGE_MATURITY},
            "triagePreview": lambda args: {
                "summary": summarize_ci_failure_triage(str(args.get("log", ""))).formatted()
            },
        }
