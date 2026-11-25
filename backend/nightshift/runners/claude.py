"""Claude Code CLI runner adapter (BLUEPRINT 5, 5c)."""

from __future__ import annotations

from pathlib import Path

from nightshift.quota.ledger import QuotaLedger
from nightshift.quota.usage import record_cli_usage
from nightshift.runners.base import RunnerResult, run_subprocess_with_timeout


class ClaudeRunner:
    """Invokes the Claude Code CLI with proven unattended flags."""

    name = "claude"

    def __init__(self, quota_ledger: QuotaLedger | None = None) -> None:
        self._quota_ledger = quota_ledger

    def build_command(
        self,
        prompt: str,
        *,
        model: str | None = None,
        effort: str = "medium",
    ) -> list[str]:
        cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions", "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
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
        cmd = self.build_command(prompt, model=model, effort=effort)
        rc, out, err, timed_out = run_subprocess_with_timeout(cmd, cwd=workdir, timeout_seconds=timeout_seconds)

        is_quota = False
        reset_at = None
        if self._quota_ledger is not None:
            is_quota = self._quota_ledger.is_quota_or_transient_error(rc, out, err)
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
