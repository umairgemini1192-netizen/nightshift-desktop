"""Deterministic fake runner used in pytest engine tests without calling external CLIs."""

from __future__ import annotations

from pathlib import Path

from nightshift.runners.base import RunnerResult


class FakeRunner:
    """Configurable in-memory runner for unit and integration tests."""

    name = "fake"

    def __init__(self, responses: list[RunnerResult] | None = None) -> None:
        self._responses = list(responses or [RunnerResult(rc=0, stdout="OK", stderr="")])
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        prompt: str,
        workdir: Path,
        *,
        model: str | None = None,
        effort: str = "medium",
        timeout_seconds: int = 3600,
    ) -> RunnerResult:
        self.calls.append(
            {
                "prompt": prompt,
                "workdir": str(workdir),
                "model": model,
                "effort": effort,
                "timeout_seconds": timeout_seconds,
            }
        )
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]
