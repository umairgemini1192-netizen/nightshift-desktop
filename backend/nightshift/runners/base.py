"""Base runner protocol and process execution contract for AI coding CLIs.

Implements least-privilege subprocess environment scrubbing and process-tree
termination per docs/AGENT_PIPELINE.md section 8 (Amendment E).

Honest trust-boundary note (§8.2):
    On Windows 11 without admin rights, every local AI CLI process runs under
    the same Windows user SID as the owner. Passing USERPROFILE / LOCALAPPDATA /
    APPDATA (required for CLI login sessions) means user-space env scrubbing and
    git worktrees are defense-in-depth, NOT a hard OS security sandbox.
"""

from __future__ import annotations

import ctypes
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

# Base environment allowlist for least-privilege runner execution (AGENT_PIPELINE §8.1).
BASE_SCRUBBED_ENV_KEYS: tuple[str, ...] = (
    "SYSTEMROOT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "PATHEXT",
    "PATH",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "LANG",
    "LC_ALL",
)

SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "AZURE_",
    "GCP_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "CIRCLE_TOKEN",
)

MAX_RUNNER_OUTPUT_BYTES: int = 10 * 1024 * 1024  # 10 MB per attempt (AGENT_PIPELINE §8.1)


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


def build_scrubbed_env(
    extra_env: dict[str, str] | None = None,
    *,
    base_env: dict[str, str] | None = None,
    allowed_keys: tuple[str, ...] = BASE_SCRUBBED_ENV_KEYS,
) -> dict[str, str]:
    """Construct a minimal allowlisted environment for a runner subprocess.

    Strips unrelated host secrets (AWS_*, OPENAI_API_KEY, GITHUB_TOKEN, GH_TOKEN, etc.)
    unless explicitly provided in ``extra_env`` for that single process.
    """
    source = os.environ if base_env is None else base_env
    scrubbed: dict[str, str] = {}
    allowed_upper = {k.upper() for k in allowed_keys}
    for key, val in source.items():
        up = key.upper()
        if up in allowed_upper:
            if any(up.startswith(pref) for pref in SENSITIVE_ENV_PREFIXES):
                continue
            scrubbed[key] = val
    scrubbed["DO_NOT_TRACK"] = "1"
    scrubbed["NO_COLOR"] = "1"
    if extra_env:
        scrubbed.update(extra_env)
    return scrubbed


def _truncate_output(text: str, max_bytes: int = MAX_RUNNER_OUTPUT_BYTES) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[TRUNCATED: output cap reached]"


def _attach_windows_kill_on_close_job(pid: int) -> int | None:
    """Attach a child process to a Windows Job Object with KILL_ON_JOB_CLOSE (no admin required).

    Returns the job handle to close when the process finishes/times out, or ``None`` if unavailable.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok_set = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok_set:
            kernel32.CloseHandle(job)
            return None

        h_proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h_proc:
            kernel32.CloseHandle(job)
            return None
        try:
            ok_assign = kernel32.AssignProcessToJobObject(job, h_proc)
            if not ok_assign:
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(h_proc)
        return int(job)
    except Exception:
        return None


def _close_windows_job(job_handle: int | None) -> None:
    if job_handle and sys.platform == "win32":
        try:
            ctypes.windll.kernel32.CloseHandle(job_handle)
        except Exception:
            pass


def run_subprocess_with_timeout(
    args: list[str],
    cwd: Path | None = None,
    timeout_seconds: int = 3600,
    env: dict[str, str] | None = None,
    *,
    scrub_env: bool = False,
    max_output_bytes: int = MAX_RUNNER_OUTPUT_BYTES,
    strip_output: bool = True,
) -> tuple[int, str, str, bool]:
    """Execute command with strict timeout, optional env scrubbing, and process-tree termination.

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

    if scrub_env:
        exec_env = build_scrubbed_env(extra_env=env)
    else:
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)

    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd) if cwd else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "env": exec_env,
        "creationflags": NO_WINDOW,
    }
    if strip_output:
        popen_kwargs.update({"text": True, "encoding": "utf-8", "errors": "replace"})

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
    except Exception as e:
        return 1, "", str(e), False

    job_handle = _attach_windows_kill_on_close_job(proc.pid)

    try:
        out, err = proc.communicate(timeout=timeout_seconds)
        if strip_output:
            raw_out = (out or "").strip()
            raw_err = (err or "").strip()
        else:
            raw_out = (out or b"").decode("utf-8", errors="replace") if isinstance(out, bytes) else (out or "")
            raw_err = (err or b"").decode("utf-8", errors="replace") if isinstance(err, bytes) else (err or "")
        return (
            proc.returncode,
            _truncate_output(raw_out, max_output_bytes),
            _truncate_output(raw_err, max_output_bytes),
            False,
        )
    except subprocess.TimeoutExpired:
        # Full process-tree kill on timeout (Job Object close + taskkill fallback on Windows)
        _close_windows_job(job_handle)
        job_handle = None
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
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return 124, (out or "").strip()[-2000:], f"Command timed out after {timeout_seconds}s", True
    except Exception as e:
        return 1, "", str(e), False
    finally:
        _close_windows_job(job_handle)


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
