"""Deterministic fake runner used in pytest engine tests without calling external CLIs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nightshift.runners.base import RunnerResult


class FakeRunner:
    """Configurable in-memory runner for unit and integration tests."""

    def __init__(
        self,
        name: str = "fake",
        responses: list[RunnerResult] | None = None,
        on_call: Callable[[str, Path, str | None, str], None] | None = None,
    ) -> None:
        self.name = name
        self._responses = list(responses or [RunnerResult(rc=0, stdout="OK", stderr="")])
        self._on_call = on_call
        self.calls: list[dict[str, object]] = []

    def set_responses(self, responses: list[RunnerResult]) -> None:
        self._responses = list(responses)

    @classmethod
    def success(cls, name: str = "fake", stdout: str = "Success") -> FakeRunner:
        return cls(name=name, responses=[RunnerResult(rc=0, stdout=stdout, stderr="")])

    @classmethod
    def no_op(cls, name: str = "fake") -> FakeRunner:
        return cls(name=name, responses=[RunnerResult(rc=0, stdout="Nothing changed.", stderr="")])

    @classmethod
    def quota_error(
        cls,
        name: str = "fake",
        reset_at: str | None = None,
        stderr: str = "Rate limit exceeded. Try again in 60 minutes.",
    ) -> FakeRunner:
        return cls(
            name=name,
            responses=[
                RunnerResult(
                    rc=1,
                    stdout="",
                    stderr=stderr,
                    quota_error=True,
                    reset_at=reset_at,
                )
            ],
        )

    @classmethod
    def timeout(cls, name: str = "fake", seconds: int = 3600) -> FakeRunner:
        return cls(
            name=name,
            responses=[
                RunnerResult(
                    rc=124,
                    stdout="",
                    stderr=f"Command timed out after {seconds}s",
                    timed_out=True,
                )
            ],
        )

    @classmethod
    def crash(cls, name: str = "fake", rc: int = 1, stderr: str = "Process crashed unexpectedly") -> FakeRunner:
        return cls(
            name=name,
            responses=[RunnerResult(rc=rc, stdout="", stderr=stderr)],
        )

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
        if self._on_call is not None:
            self._on_call(prompt, workdir, model, effort)

        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]
