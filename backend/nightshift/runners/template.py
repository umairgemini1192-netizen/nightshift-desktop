"""Template runner adapter for dynamic engines configured in the catalogue (BLUEPRINT 5c, 5h)."""

from __future__ import annotations

import json
from pathlib import Path

from nightshift.quota.ledger import QuotaLedger
from nightshift.quota.usage import record_cli_usage
from nightshift.runners.base import RunnerResult, run_subprocess_with_timeout


class TemplateRunner:
    """Executes a command template safely without shell interpolation."""

    def __init__(
        self,
        name: str,
        command_template: list[str] | str,
        *,
        quota_ledger: QuotaLedger | None = None,
        quota_patterns: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        if isinstance(command_template, str):
            try:
                self.template = json.loads(command_template)
            except Exception:
                self.template = [command_template]
        else:
            self.template = list(command_template)
        self._quota_ledger = quota_ledger
        self._quota_patterns = quota_patterns or []
        self._env = env or {}

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        *,
        model: str | None = None,
        effort: str = "medium",
    ) -> list[str]:
        cmd: list[str] = []
        for token in self.template:
            sub = token.replace("{instruction}", prompt)
            sub = sub.replace("{prompt}", prompt)
            sub = sub.replace("{model}", model or "")
            sub = sub.replace("{effort}", effort or "medium")
            sub = sub.replace("{workdir}", str(workdir))
            cmd.append(sub)
        return cmd

    async def run(
        self,
        prompt: str,
        workdir: Path,
        *,
        model: str | None = None,
        effort: str = "medium",
        timeout_seconds: int = 3600,
    ) -> RunnerResult:
        cmd = self.build_command(prompt, workdir, model=model, effort=effort)
        rc, out, err, timed_out = run_subprocess_with_timeout(
            cmd, cwd=workdir, timeout_seconds=timeout_seconds, env=self._env, scrub_env=True
        )

        is_quota = False
        reset_at = None
        if self._quota_ledger is not None:
            is_quota = self._quota_ledger.is_quota_or_transient_error(rc, out, err, custom_patterns=self._quota_patterns)
            if is_quota:
                reset_at = self._quota_ledger.record_quota_error(self.name, model or "default", f"{out}\n{err}")
            else:
                record_cli_usage(self._quota_ledger, self.name, model or "default", out)

        return RunnerResult(
            rc=rc,
            stdout=out,
            stderr=err,
            timed_out=timed_out,
            quota_error=is_quota,
            reset_at=reset_at,
            command=cmd,
        )
