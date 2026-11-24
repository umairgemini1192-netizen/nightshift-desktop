"""Base runner protocol and process execution contract for AI coding CLIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RunnerResult:
    """Outcome of a single headless AI CLI invocation."""

    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    quota_error: bool = False
    reset_at: str | None = None


class Runner(Protocol):
    """Protocol implemented by every AI CLI adapter."""

    name: str

    async def run(
        self,
        prompt: str,
        workdir: Path,
        *,
        model: str | None = None,
        effort: str = "medium",
        timeout_seconds: int = 3600,
    ) -> RunnerResult:
        """Execute a single prompt inside ``workdir`` and return the captured result."""
        ...
