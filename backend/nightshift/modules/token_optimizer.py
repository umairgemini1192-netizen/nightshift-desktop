"""Token Optimizer backend module (BLUEPRINT sections 4 & 5g)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nightshift.events import Event, EventBus
from nightshift.modules.base import ModuleBase
from nightshift.optimizer.ab_framework import ABFramework
from nightshift.optimizer.agy_fast import probe_agy_isolation
from nightshift.optimizer.tool_manager import ToolManager
from nightshift.store import Store


class TokenOptimizerService:
    """Coordinates tool verification, agy-fast isolation probe, and A/B toggles."""

    def __init__(
        self,
        store: Store | None = None,
        events: EventBus | None = None,
        *,
        tools_root: Path | None = None,
        lockfile_path: Path | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._tools_root = tools_root
        self._lockfile_path = lockfile_path
        self._manager = ToolManager(store=store, tools_root=tools_root, lockfile_path=lockfile_path)

    def attach(self, store: Store | None, events: EventBus | None = None) -> None:
        self._store = store
        self._events = events
        self._manager = ToolManager(
            store=store,
            tools_root=self._tools_root,
            lockfile_path=self._lockfile_path,
        )

    @property
    def manager(self) -> ToolManager:
        return self._manager

    def get_state(self) -> dict[str, Any]:
        """Return full tool catalogue, verification states, isolation status, and A/B savings."""
        ab = ABFramework(self._store, self._events) if self._store is not None else None
        tonight = ab.summarize_tonight() if ab is not None else {
            "totalTokensSaved": 0,
            "estimateLabel": "~0 tokens saved tonight (estimate)",
            "perTool": [],
            "warnings": [],
            "isEstimate": True,
        }
        per_tool_map = {item["tool"]: item for item in tonight.get("perTool", [])}
        tools_payload: list[dict[str, Any]] = []
        warnings: list[str] = list(tonight.get("warnings", []))

        for spec in self._manager.list_specs():
            db_state = self._store.get_token_tool_state(spec.name) if self._store is not None else None
            ab_info = per_tool_map.get(spec.name, {})
            enabled = bool(db_state.get("enabled", 0)) if db_state else False
            sha_ok = bool(db_state.get("sha256_verified", 0)) if db_state else False
            iso_passed = bool(db_state.get("isolation_passed", 0)) if db_state else False
            iso_method = str(db_state.get("isolation_method", "")) if db_state else ""
            auto_disabled = bool(db_state.get("auto_disabled", 0)) if db_state else False
            warning = str(db_state.get("warning", "")) if db_state else ""

            if spec.catalogue_only:
                status = "catalogue_only"
                enabled = False
                if not warning:
                    warning = "Catalogue entry only (disabled). Enable later if ccusage shows search is a top cost."
            elif auto_disabled:
                status = "auto_disabled"
            elif spec.isolation_gate_required and not iso_passed:
                status = "isolation_blocked"
                enabled = False
            elif warning and not sha_ok:
                status = "hash_mismatch"
                enabled = False
            elif enabled:
                status = "enabled"
                # Verify tool installation (P1 fix: ensure tools are installed before marking enabled)
                if spec.name != "agy-fast":
                    verify_res = self._manager.verify_installed(spec.name)
                    if not verify_res.ok:
                        warning = verify_res.warning or "Tool verification failed after enable"
                        status = "hash_mismatch"
            else:
                status = "disabled"

            if warning and warning not in warnings and not spec.catalogue_only:
                warnings.append(warning)

            tools_payload.append(
                {
                    "name": spec.name,
                    "displayName": spec.display_name,
                    "role": spec.role,
                    "version": spec.version,
                    "artifact": spec.artifact,
                    "sha256": spec.sha256,
                    "license": spec.license,
                    "runners": list(spec.runners),
                    "catalogueOnly": spec.catalogue_only,
                    "isolationGateRequired": spec.isolation_gate_required,
                    "isolationPassed": iso_passed,
                    "isolationMethod": iso_method or "none",
                    "sha256Verified": sha_ok,
                    "enabled": enabled,
                    "autoDisabled": auto_disabled,
                    "status": status,
                    "warning": warning,
                    "tokensSavedTonight": int(ab_info.get("tokensSavedTonight", 0)),
                    "estimateLabel": str(ab_info.get("estimateLabel", "~0 tokens saved (estimate)")),
                    "abAction": str(ab_info.get("abAction", "keep_measuring")),
                    "abReason": str(ab_info.get("abReason", "")),
                }
            )

        return {
            "tools": tools_payload,
            "totalTokensSaved": int(tonight.get("totalTokensSaved", 0)),
            "estimateLabel": str(tonight.get("estimateLabel", "~0 tokens saved tonight (estimate)")),
            "warnings": warnings,
            "toolsRoot": str(self._manager.tools_root),
            "telemetryOff": True,
            "isEstimate": True,
        }

    def set_tool_enabled(self, tool_name: str, enabled: bool) -> dict[str, Any]:
        """Toggle a tool on/off while enforcing catalogue, isolation, and SHA-256 gates."""
        if self._store is None:
            raise RuntimeError("Store is not attached")
        spec = self._manager.get_spec(tool_name)
        if spec is None:
            raise ValueError(f"Unknown token optimizer tool: {tool_name}")

        if enabled:
            if spec.catalogue_only:
                msg = f"{spec.display_name} is a catalogue-only entry and remains disabled until search is a top cost."
                self._store.upsert_token_tool_state(tool_name, enabled=False, warning=msg)
                return self.get_state()
            policy_err = self._manager.validate_policy(spec)
            if policy_err:
                self._store.upsert_token_tool_state(tool_name, enabled=False, warning=policy_err)
                return self.get_state()
            if spec.isolation_gate_required:
                cur = self._store.get_token_tool_state(tool_name) or {}
                if not bool(cur.get("isolation_passed", 0)):
                    msg = (
                        f"{spec.display_name} cannot be enabled until the isolation probe confirms "
                        "it runs without modifying ~/.gemini/config."
                    )
                    self._store.upsert_token_tool_state(tool_name, enabled=False, warning=msg)
                    return self.get_state()

        self._store.upsert_token_tool_state(
            tool_name,
            enabled=enabled,
            auto_disabled=False if enabled else None,
            warning="" if not enabled else None,
        )
        if self._events is not None:
            state_word = "enabled" if enabled else "disabled"
            self._events.emit(
                Event(
                    kind="token_optimizer.toggled",
                    plain_text=f"Token optimizer tool {spec.display_name} {state_word}.",
                    level="info",
                    payload={"tool": tool_name, "enabled": enabled},
                )
            )
        return self.get_state()

    def verify_tools(self) -> dict[str, Any]:
        """Verify SHA-256 hashes of all installed tools against tools.lock.json."""
        results: list[dict[str, Any]] = []
        for spec in self._manager.list_specs():
            if spec.catalogue_only:
                continue
            res = self._manager.verify_installed(spec.name)
            results.append(
                {
                    "tool": spec.name,
                    "ok": res.ok,
                    "warning": res.warning,
                }
            )
            if not res.ok and res.actual_sha256 and self._events is not None:
                self._events.emit(
                    Event(
                        kind="token_optimizer.hash_mismatch",
                        plain_text=res.warning,
                        level="warn",
                        payload={"tool": spec.name},
                    )
                )
        state = self.get_state()
        state["verificationResults"] = results
        return state

    def run_isolation_probe(self) -> dict[str, Any]:
        """Run the automated agy-fast isolation probe and persist the result."""
        result = probe_agy_isolation(tool_manager=self._manager)
        if self._store is not None:
            self._store.upsert_token_tool_state(
                "agy-fast",
                enabled=False if not result.passed else None,
                isolation_passed=result.passed,
                isolation_method=result.method,
                warning="" if result.passed else result.reason,
            )
        if self._events is not None:
            self._events.emit(
                Event(
                    kind="token_optimizer.isolation_probe",
                    plain_text=result.reason,
                    level="info" if result.passed else "warn",
                    payload={"passed": result.passed, "method": result.method},
                )
            )
        state = self.get_state()
        state["probeResult"] = {
            "passed": result.passed,
            "method": result.method,
            "reason": result.reason,
            "globalConfigModified": result.global_config_modified,
            "candidatesTried": list(result.candidates_tried),
        }
        return state

    def evaluate_ab(self, phase_kind: str = "default") -> dict[str, Any]:
        """Evaluate A/B metrics for all enabled tools and auto-disable regressions."""
        if self._store is None:
            return self.get_state()
        ab = ABFramework(self._store, self._events)
        decisions: list[dict[str, Any]] = []
        for spec in self._manager.list_specs():
            if spec.catalogue_only:
                continue
            dec = ab.evaluate_and_apply(spec.name, phase_kind)
            decisions.append(dec.to_dict())
        state = self.get_state()
        state["decisions"] = decisions
        return state


class Module(ModuleBase):
    """Pluggable ``token_optimizer`` module (BLUEPRINT sections 4 & 5g)."""

    id = "token_optimizer"

    def __init__(self) -> None:
        self.service = TokenOptimizerService()

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
            "getState": lambda _args: self.service.get_state(),
            "setToolEnabled": lambda args: self.service.set_tool_enabled(
                str(args.get("tool", "")),
                bool(args.get("enabled", False)),
            ),
            "verifyTools": lambda _args: self.service.verify_tools(),
            "runIsolationProbe": lambda _args: self.service.run_isolation_probe(),
            "evaluateAb": lambda args: self.service.evaluate_ab(
                phase_kind=str(args.get("phaseKind", "default")),
            ),
        }
