"""Base runner protocol and process execution contract for AI coding CLIs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# The packaged app is windowed (no console). Without this flag every CLI it starts
# (gh, git, agy, claude, ...) pops up its own console window on the owner's screen.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class RunnerResult:
    """Outcome of a single headless AI CLI invocation."""

    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    quota_error: bool = False
    reset_at: str | None = None
    command: list[str] = field(default_factory=list)


def resolve_bin(name: str) -> str | None:
    """Resolve an executable binary on PATH, supporting Windows extensions."""
    if sys.platform == "win32":
        candidates = (f"{name}.exe", f"{name}.cmd", f"{name}.bat", name, f"{name}.ps1")
    else:
        candidates = (name,)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def run_subprocess_with_timeout(
    args: list[str],
    cwd: Path | None = None,
    timeout_seconds: int = 3600,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """Execute command with strict timeout and full process-tree termination.

    Returns (returncode, stdout, stderr, timed_out).
    """
    if not args:
        return 1, "", "No command arguments provided", False

    exe = resolve_bin(args[0])
    if not exe:
        return 127, "", f"Executable not found in PATH: {args[0]}", False

    if exe.lower().endswith(".ps1"):
        ps = shutil.which("powershell.exe") or "powershell.exe"
        cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe] + args[1:]
    else:
        cmd = [exe] + args[1:]

    exec_env = os.environ.copy()
    if env:
        exec_env.update(env)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=exec_env,
            creationflags=NO_WINDOW,
        )
    except Exception as e:
        return 1, "", str(e), False

    try:
        out, err = proc.communicate(timeout=timeout_seconds)
        return proc.returncode, (out or "").strip(), (err or "").strip(), False
    except subprocess.TimeoutExpired:
        # Full process-tree kill on timeout (agents spawn node/gradle/git children)
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
        else:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        return 124, (out or "").strip()[-2000:], f"Command timed out after {timeout_seconds}s", True
    except Exception as e:
        return 1, "", str(e), False


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
