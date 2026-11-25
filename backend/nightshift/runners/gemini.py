"""Gemini CLI runner adapter (BLUEPRINT 5, 5c)."""

from __future__ import annotations

from pathlib import Path

from nightshift.quota.ledger import QuotaLedger
from nightshift.quota.usage import record_cli_usage
from nightshift.runners.base import RunnerResult, run_subprocess_with_timeout


class GeminiRunner:
    """Invokes the Gemini CLI with proven unattended flags."""

    name = "gemini"

    def __init__(self, quota_ledger: QuotaLedger | None = None) -> None:
        self._quota_ledger = quota_ledger

    def build_command(
        self,
        prompt: str,
        *,
        model: str | None = None,
        effort: str = "medium",
    ) -> list[str]:
        # --skip-trust: headless runs in fresh worktrees are otherwise "untrusted" and exit 55.
        cmd = ["gemini", "-p", prompt, "--approval-mode=yolo", "--skip-trust"]
        if model:
            cmd.extend(["--model", model])
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
            elif rc == 0 and not timed_out:
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
