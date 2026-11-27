"""ccusage integration: pinned --offline --json parser with CLI cross-check & fallback (BLUEPRINT 5g)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.optimizer.tool_manager import ToolManager
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.base import run_subprocess_with_timeout

CommandExecutorFn = Callable[[list[str], Path | None, dict[str, str], int], tuple[int, str, str, bool]]


def _default_exec(
    cmd: list[str],
    cwd: Path | None,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, str, str, bool]:
    return run_subprocess_with_timeout(cmd, cwd=cwd, timeout_seconds=timeout_seconds, env=env)


@dataclass(frozen=True)
class EngineModelUsage:
    """Normalized usage row parsed from ccusage --offline --json."""

    engine: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float = 0.0
    source: str = "ccusage"


@dataclass(frozen=True)
class CcusageSyncResult:
    """Outcome of syncing ccusage metrics into the QuotaLedger."""

    ok: bool
    records: list[EngineModelUsage] = field(default_factory=list)
    used_fallback_engines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "ccusage"


def _normalize_engine_name(raw: str) -> str:
    lowered = raw.strip().lower()
    if lowered in ("agy", "antigravity", "antigravity-cli"):
        return "agy"
    if lowered in ("claude", "claude-code", "anthropic"):
        return "claude"
    if lowered in ("gemini", "gemini-cli", "google"):
        return "gemini"
    if lowered in ("codex", "codex-cli", "openai", "gpt"):
        return "codex"
    return lowered or "unknown"


def parse_ccusage_json(raw_json: str) -> list[EngineModelUsage]:
    """Parse ccusage --offline --json output into normalized per-engine/model records.

    Supports both top-level ``{"engines": [...]}`` / ``{"models": [...]}`` and
    ``{"daily": [{"modelBreakdowns": [...]}]}`` schemas emitted by ccusage.
    """
    if not raw_json or not raw_json.strip():
        return []
    data = json.loads(raw_json)
    records: list[EngineModelUsage] = []

    if isinstance(data, dict):
        # Schema A: {"entries": [...]} or {"engines": [...]} or {"models": [...]}
        list_items = data.get("entries") or data.get("engines") or data.get("models")
        if isinstance(list_items, list):
            for item in list_items:
                if not isinstance(item, dict):
                    continue
                engine = _normalize_engine_name(str(item.get("engine") or item.get("runner") or item.get("provider") or "claude"))
                model = str(item.get("model") or item.get("modelName") or "default")
                t_in = int(item.get("inputTokens") or item.get("tokens_in") or item.get("input_tokens") or 0)
                t_out = int(item.get("outputTokens") or item.get("tokens_out") or item.get("output_tokens") or 0)
                cost = float(item.get("totalCost") or item.get("cost_usd") or item.get("cost") or 0.0)
                if t_in > 0 or t_out > 0:
                    records.append(EngineModelUsage(engine=engine, model=model, tokens_in=t_in, tokens_out=t_out, cost_usd=cost))

        # Schema B: {"daily": [{"modelBreakdowns": [...]}]}
        daily = data.get("daily")
        if isinstance(daily, list) and not records:
            for day in daily:
                if not isinstance(day, dict):
                    continue
                breakdowns = day.get("modelBreakdowns") or []
                if isinstance(breakdowns, list) and breakdowns:
                    for br in breakdowns:
                        if not isinstance(br, dict):
                            continue
                        engine = _normalize_engine_name(str(br.get("engine") or br.get("provider") or "claude"))
                        model = str(br.get("modelName") or br.get("model") or "default")
                        t_in = int(br.get("inputTokens") or br.get("input_tokens") or 0)
                        t_out = int(br.get("outputTokens") or br.get("output_tokens") or 0)
                        cost = float(br.get("cost") or br.get("totalCost") or 0.0)
                        if t_in > 0 or t_out > 0:
                            records.append(EngineModelUsage(engine=engine, model=model, tokens_in=t_in, tokens_out=t_out, cost_usd=cost))
                else:
                    t_in = int(day.get("inputTokens") or 0)
                    t_out = int(day.get("outputTokens") or 0)
                    if t_in > 0 or t_out > 0:
                        records.append(EngineModelUsage(engine="claude", model="default", tokens_in=t_in, tokens_out=t_out, cost_usd=float(day.get("totalCost") or 0.0)))

    return records


class CcusageAdapter:
    """Runs pinned ccusage --offline --json, cross-checks against CLI JSON usage, and falls back safely."""

    def __init__(
        self,
        tool_manager: ToolManager,
        quota_ledger: QuotaLedger,
        *,
        executor: CommandExecutorFn | None = None,
        drift_tolerance: float = 0.25,
    ) -> None:
        self._tool_manager = tool_manager
        self._quota_ledger = quota_ledger
        self._executor = executor or _default_exec
        self._drift_tolerance = drift_tolerance

    def sync_usage(
        self,
        *,
        cli_usage_by_engine: dict[str, tuple[int, int]] | None = None,
        expect_antigravity: bool = True,
        workdir: Path | None = None,
        timeout_seconds: int = 30,
    ) -> CcusageSyncResult:
        """Collect usage via ccusage --offline --json, cross-check, and fall back when missing or off."""
        cli_map = cli_usage_by_engine or {}
        ver = self._tool_manager.verify_installed("ccusage")
        if not ver.ok or ver.executable_path is None:
            warnings = [ver.warning or "ccusage is unavailable; falling back to CLI JSON usage and run counts."]
            fallback_records = self._record_fallback(cli_map)
            return CcusageSyncResult(
                ok=False,
                records=fallback_records,
                used_fallback_engines=sorted(cli_map.keys()) or (["agy"] if expect_antigravity else []),
                warnings=warnings,
                source="cli_fallback",
            )

        spec = self._tool_manager.get_spec("ccusage")
        flags = list(spec.flags) if spec else ["--offline", "--json"]
        if "--offline" not in flags:
            flags.append("--offline")
        if "--json" not in flags:
            flags.append("--json")

        cmd = [str(ver.executable_path), *flags]
        env = self._tool_manager.build_process_env()
        rc, stdout, stderr, timed_out = self._executor(cmd, workdir, env, timeout_seconds)
        if rc != 0 or timed_out:
            reason = "timed out" if timed_out else (stderr[:160] or f"exit code {rc}")
            warnings = [f"ccusage failed ({reason}); falling back to CLI JSON usage and run counts."]
            fallback_records = self._record_fallback(cli_map)
            return CcusageSyncResult(
                ok=False,
                records=fallback_records,
                used_fallback_engines=sorted(cli_map.keys()) or (["agy"] if expect_antigravity else []),
                warnings=warnings,
                source="cli_fallback",
            )

        try:
            parsed = parse_ccusage_json(stdout)
        except Exception as exc:
            warnings = [f"Could not parse ccusage output ({exc}); falling back to CLI JSON usage and run counts."]
            fallback_records = self._record_fallback(cli_map)
            return CcusageSyncResult(
                ok=False,
                records=fallback_records,
                used_fallback_engines=sorted(cli_map.keys()) or (["agy"] if expect_antigravity else []),
                warnings=warnings,
                source="cli_fallback",
            )

        # Group ccusage totals per engine for cross-checking against CLI JSON usage.
        by_engine: dict[str, list[EngineModelUsage]] = {}
        for rec in parsed:
            by_engine.setdefault(rec.engine, []).append(rec)

        final_records: list[EngineModelUsage] = []
        fallback_engines: list[str] = []
        warnings: list[str] = []

        engines_to_check = set(by_engine.keys()) | set(cli_map.keys())
        if expect_antigravity:
            engines_to_check.add("agy")

        for eng in sorted(engines_to_check):
            cc_rows = by_engine.get(eng, [])
            cc_total = sum(r.tokens_in + r.tokens_out for r in cc_rows)
            cli_pair = cli_map.get(eng)
            cli_total = (cli_pair[0] + cli_pair[1]) if cli_pair else 0

            if not cc_rows:
                if eng == "agy" and expect_antigravity:
                    warnings.append(
                        "ccusage did not report Antigravity usage; falling back to CLI JSON usage and run counts for Antigravity."
                    )
                    fallback_engines.append("agy")
                elif cli_total > 0:
                    warnings.append(
                        f"ccusage had no data for {eng}; falling back to CLI JSON usage for {eng}."
                    )
                    fallback_engines.append(eng)
                if cli_pair and cli_total > 0:
                    fb = EngineModelUsage(
                        engine=eng,
                        model="default",
                        tokens_in=cli_pair[0],
                        tokens_out=cli_pair[1],
                        source="cli_fallback",
                    )
                    final_records.append(fb)
                    self._quota_ledger.record_usage(eng, "default", cli_pair[0], cli_pair[1], event="cli_fallback")
                continue

            # Cross-check ccusage against CLI JSON usage when both are present.
            if cli_pair and cli_total > 0:
                drift = abs(cc_total - cli_total) / max(cli_total, 1)
                if drift > self._drift_tolerance:
                    pct = int(round(drift * 100))
                    label = "Antigravity" if eng == "agy" else eng
                    warnings.append(
                        f"ccusage numbers for {label} differed from CLI JSON usage by {pct}%; "
                        f"falling back to CLI JSON usage for {label}."
                    )
                    fallback_engines.append(eng)
                    fb = EngineModelUsage(
                        engine=eng,
                        model="default",
                        tokens_in=cli_pair[0],
                        tokens_out=cli_pair[1],
                        source="cli_fallback",
                    )
                    final_records.append(fb)
                    self._quota_ledger.record_usage(eng, "default", cli_pair[0], cli_pair[1], event="cli_fallback")
                    continue

            for row in cc_rows:
                final_records.append(row)
                self._quota_ledger.record_usage(
                    row.engine,
                    row.model,
                    row.tokens_in,
                    row.tokens_out,
                    event="ccusage",
                )

        source = "mixed" if (fallback_engines and any(r.source == "ccusage" for r in final_records)) else (
            "cli_fallback" if fallback_engines and not any(r.source == "ccusage" for r in final_records) else "ccusage"
        )
        return CcusageSyncResult(
            ok=True,
            records=final_records,
            used_fallback_engines=fallback_engines,
            warnings=warnings,
            source=source,
        )

    def _record_fallback(self, cli_map: dict[str, tuple[int, int]]) -> list[EngineModelUsage]:
        records: list[EngineModelUsage] = []
        for eng, (t_in, t_out) in sorted(cli_map.items()):
            if t_in > 0 or t_out > 0:
                self._quota_ledger.record_usage(eng, "default", t_in, t_out, event="cli_fallback")
                records.append(
                    EngineModelUsage(
                        engine=eng,
                        model="default",
                        tokens_in=t_in,
                        tokens_out=t_out,
                        source="cli_fallback",
                    )
                )
        return records
