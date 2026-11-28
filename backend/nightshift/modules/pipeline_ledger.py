"""Pipeline Ledger & Scores backend module (AGENT_PIPELINE §11.2, Amendment I & J)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from nightshift.engine.pipeline_economics import (
    GRADUATION_MIN_SAMPLES,
    PIPELINE_STAGES,
    PipelineEconomics,
    compute_entry_total_cost_usd_est,
    min_threshold_for_risk,
)
from nightshift.engine.pipeline_review import tier_for_engine
from nightshift.events import Event, EventBus
from nightshift.modules.base import ModuleBase
from nightshift.store import Store

_STATUS_TO_STAGE_INDEX: dict[str, int] = {
    "QUEUED": 0,      # PLAN done, awaiting CODE
    "RUNNING": 1,     # CODE / FIX
    "VERIFYING": 2,   # MECHANICAL / TRIAGE
    "REVIEWING": 5,   # REVIEW
    "APPROVED": 6,    # GATES done, awaiting MERGE
    "MERGED": 7,      # MERGE done, awaiting RELEASE
    "RELEASED": 8,    # RELEASE done
}


def _build_stage_segments(task: dict[str, Any], attempts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Map a task's durable status & attempts to the 9-stage pipeline progress bar."""
    status = str(task.get("status") or "QUEUED").upper()
    blocked_from = str(task.get("blocked_from_status") or "").upper()
    effective_status = blocked_from if status == "BLOCKED" and blocked_from else status
    active_idx = _STATUS_TO_STAGE_INDEX.get(effective_status, 0)
    attempted_stages = {str(a.get("stage") or "").upper() for a in attempts}

    segments: list[dict[str, str]] = []
    for idx, st in enumerate(PIPELINE_STAGES):
        sid = st["id"]
        if status == "RELEASED":
            seg_status = "done"
        elif status == "FAILED":
            seg_status = "done" if idx < active_idx else ("failed" if idx == active_idx else "pending")
        elif status == "BLOCKED":
            seg_status = "done" if idx < active_idx else ("failed" if idx == active_idx else "pending")
        else:
            if idx < active_idx:
                if sid in ("TRIAGE", "FIX") and sid not in attempted_stages:
                    seg_status = "done"
                else:
                    seg_status = "done"
            elif idx == active_idx:
                seg_status = "running"
            else:
                seg_status = "pending"
        segments.append(
            {
                "id": sid,
                "label": st["label"],
                "role": st["role"],
                "executor": st["executor"],
                "status": seg_status,
            }
        )
    return segments


class PipelineLedgerService:
    """Builds the Pipeline Ledger & Scores payload from SQLite tables."""

    def __init__(self, store: Store | None = None, events: EventBus | None = None) -> None:
        self._store = store
        self._events = events

    def attach(self, store: Store | None, events: EventBus | None = None) -> None:
        self._store = store
        self._events = events

    def get_overview(self, *, project_id: str | None = None) -> dict[str, Any]:
        if self._store is None:
            return {
                "summary": {
                    "acceptedChanges30d": 0,
                    "costPerAcceptedUsdEst": 0.0,
                    "regressionRatePct": 0.0,
                    "goldSavedTasks": 0,
                    "freeOnlyMode": True,
                    "monthlyBudget": {"spentUsdEst": 0.0, "capUsdEst": 0.0, "monthLabel": "", "isEstimate": True},
                    "tonightBudget": {
                        "freeOnlyMode": True,
                        "spentUsdEst": 0.0,
                        "capUsdEst": 0.0,
                        "invocations": 0,
                        "maxInvocations": 60,
                        "wallHours": 0.0,
                        "maxHours": 8.0,
                        "displayLine": "$0.00 / $0.00 (free-only mode)",
                        "isEstimate": True,
                    },
                    "isEstimate": True,
                },
                "activeTasks": [],
                "modelScores": [],
                "taskLedger": [],
                "stageMaturity": [],
                "releaseVerifications": [],
            }

        econ = PipelineEconomics(self._store, self._events)
        summary = econ.get_30d_summary(project_id=project_id)
        stage_maturity = econ.get_stage_maturity()
        overall_maturity = stage_maturity[0]["maturity"] if stage_maturity else "Integration-tested"

        tasks = self._store.list_tasks(project_id=project_id)
        active_tasks: list[dict[str, Any]] = []
        task_ledger: list[dict[str, Any]] = []

        for t in tasks:
            tid = str(t["id"])
            attempts = self._store.list_task_attempts(task_id=tid, limit=50)
            evidence = self._store.list_evidence_for_task(tid)
            ledger_rows = self._store.list_pipeline_ledger(task_id=tid, limit=100)
            try:
                contrib_providers = list(json.loads(str(t.get("contributing_providers_json") or "[]")))
            except Exception:
                contrib_providers = []

            tokens_total = sum(int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in ledger_rows)
            ci_minutes = round(sum(float(r.get("ci_minutes") or 0.0) for r in ledger_rows), 2)
            cost_usd_est = round(sum(compute_entry_total_cost_usd_est(r) for r in ledger_rows), 4)
            wall_seconds = round(sum(float(r.get("wall_seconds") or 0.0) for r in ledger_rows), 1)
            reviewer_provider = next(
                (str(r.get("provider") or "") for r in ledger_rows if str(r.get("role") or "") == "reviewer"),
                None,
            )

            ev_items = [
                {
                    "id": int(ev["id"]),
                    "kind": str(ev["kind"]),
                    "source": str(ev["source"]),
                    "commitSha": str(ev["commit_sha"])[:7],
                    "durationSeconds": float(ev.get("duration_seconds") or 0.0),
                    "verified": bool(ev.get("verified")),
                    "rejectionReason": str(ev.get("rejection_reason") or ""),
                }
                for ev in evidence
            ]

            attempt_items = [
                {
                    "id": int(a["id"]),
                    "stage": str(a["stage"]),
                    "role": str(a["role"]),
                    "engineId": str(a["engine_id"]),
                    "provider": str(a["provider"]),
                    "outcome": str(a["outcome"]),
                    "wallSeconds": float(a.get("wall_seconds") or 0.0),
                    "decisionReason": str(a.get("decision_reason") or ""),
                    "outputRef": str(a.get("output_ref") or ""),
                }
                for a in attempts
            ]

            task_payload = {
                "id": tid,
                "phaseId": str(t["phase_id"]),
                "title": str(t["title"]),
                "branch": str(t["branch"]),
                "status": str(t["status"]),
                "riskClass": str(t["risk_class"]),
                "prNumber": t.get("pr_number"),
                "headSha": str(t.get("head_sha") or ""),
                "contractSha256": str(t.get("contract_sha256") or "")[:12],
                "contributingProviders": contrib_providers,
                "reviewerProvider": reviewer_provider,
                "maturity": overall_maturity,
                "stages": _build_stage_segments(t, attempts),
                "evidence": ev_items,
                "tokensEstimate": tokens_total,
                "ciMinutesEstimate": ci_minutes,
                "costUsdEstimate": cost_usd_est,
                "wallSeconds": wall_seconds,
                "attempts": attempt_items,
                "blockReason": str(t.get("block_reason") or ""),
                "blockDetail": str(t.get("block_detail") or ""),
            }
            task_ledger.append(task_payload)
            if str(t["status"]) not in ("RELEASED",):
                active_tasks.append(task_payload)

        # Model scores
        raw_scores = self._store.list_model_scores()
        model_scores: list[dict[str, Any]] = []
        for s in raw_scores:
            eng_id = str(s["engine_id"])
            eng_row = self._store.get_engine(eng_id)
            tier = tier_for_engine(eng_id, explicit_tier=str(eng_row.get("cost_tier") or "") if eng_row else None)
            samples = int(s.get("sample_tasks") or 0)
            accepted = int(s.get("accepted_tasks") or 0)
            reg_count = int(s.get("regression_count") or 0)
            acc_rate = round((accepted / samples) * 100.0, 1) if samples > 0 else 0.0
            reg_denom = accepted + reg_count
            reg_rate = round((reg_count / reg_denom) * 100.0, 1) if reg_denom > 0 else 0.0
            graduated = bool(int(s.get("benchmark_graduated") or 0) == 1)
            thresh = min_threshold_for_risk(str(s["risk_class"]))
            if tier != "economy" or graduated:
                grad_status = "graduated"
                grad_label = "Graduated"
            elif samples >= GRADUATION_MIN_SAMPLES:
                grad_status = "demoted"
                grad_label = "Demoted"
            else:
                grad_status = "benchmarking"
                grad_label = f"Benchmarking ({samples}/{GRADUATION_MIN_SAMPLES})"

            model_scores.append(
                {
                    "engineId": eng_id,
                    "model": str(s["model"]),
                    "role": str(s["role"]),
                    "riskClass": str(s["risk_class"]),
                    "tier": tier,
                    "sampleTasks": samples,
                    "acceptedTasks": accepted,
                    "acceptanceRatePct": acc_rate,
                    "avgRetries": round(float(s.get("avg_retries") or 0.0), 2),
                    "regressionRatePct": reg_rate,
                    "costPerAcceptedUsdEst": round(float(s.get("cost_per_accepted_usd_est") or 0.0), 4),
                    "benchmarkGraduated": graduated,
                    "graduationStatus": grad_status,
                    "graduationLabel": grad_label,
                    "thresholdPct": int(round(thresh * 100)),
                    "isEstimate": True,
                }
            )

        releases = self._store.list_release_verifications(project_id=project_id, limit=15)
        release_items = [
            {
                "tag": str(r["tag"]),
                "repo": str(r["repo"]),
                "taskId": r.get("task_id"),
                "mergeSha": str(r.get("merge_sha") or "")[:7],
                "previousGoodTag": r.get("previous_good_tag"),
                "smokeStatus": str(r.get("smoke_status") or "pending"),
                "smokeDetail": str(r.get("smoke_detail") or ""),
                "verifiedAt": r.get("verified_at"),
                "rolledBackAt": r.get("rolled_back_at"),
            }
            for r in releases
        ]

        return {
            "summary": summary,
            "activeTasks": active_tasks,
            "modelScores": model_scores,
            "taskLedger": task_ledger,
            "stageMaturity": stage_maturity,
            "releaseVerifications": release_items,
        }

    def set_free_only_mode(self, enabled: bool) -> dict[str, Any]:
        if self._store is None:
            raise RuntimeError("Store is not attached")
        self._store.set_setting("pipeline.freeOnlyMode", "1" if enabled else "0")
        if self._events is not None:
            mode_str = "enabled (zero paid API spend)" if enabled else "disabled (paid API caps apply)"
            self._events.emit(
                Event(
                    kind="pipeline.free_only_toggled",
                    plain_text=f"Free-only mode {mode_str}.",
                    level="info",
                )
            )
        return {"freeOnlyMode": enabled}


class Module(ModuleBase):
    """Pluggable ``pipeline_ledger`` backend module (BLUEPRINT §4, AGENT_PIPELINE §11.2)."""

    id = "pipeline_ledger"

    def __init__(self) -> None:
        self.service = PipelineLedgerService()

    def configure(
        self,
        *,
        store: Any = None,
        events: Any = None,
        creds: Any = None,
        engine: Any = None,
    ) -> None:
        _ = (creds, engine)
        self.service.attach(store, events)

    def api(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "getOverview": lambda args: self.service.get_overview(
                project_id=str(args.get("projectId") or "").strip() or None,
            ),
            "setFreeOnlyMode": lambda args: self.service.set_free_only_mode(
                bool(args.get("enabled", True)),
            ),
        }
