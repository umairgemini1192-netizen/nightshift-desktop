"""Per-user Windows autostart via Task Scheduler (no administrator rights).

Creates or deletes a per-user ONLOGON scheduled task with /RL LIMITED so
Night-Shift can launch minimized to the system tray on Windows sign-in without
requiring elevation, registry HKLM access, or Windows services.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

TASK_NAME = "NightShiftDesktop"


@dataclass(frozen=True)
class CommandResult:
    """Minimal command execution result for injectable testability."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str]], CommandResult]


def default_command_runner(cmd: list[str]) -> CommandResult:
    """Execute a command without a shell and without flashing a console window."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            creationflags=creationflags,
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    except OSError as exc:
        return CommandResult(returncode=1, stderr=str(exc))


def resolve_executable_command(exe_path: Path | None = None) -> str:
    """Return the quoted command line used by the per-user scheduled task."""
    if exe_path is not None:
        return f'"{exe_path}" --minimized'
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --minimized'
    return f'"{Path(sys.executable).resolve()}" -m nightshift.app --minimized'


class AutostartManager:
    """Manages per-user logon autostart via ``schtasks`` (no admin)."""

    def __init__(
        self,
        *,
        task_name: str = TASK_NAME,
        exe_path: Path | None = None,
        runner: CommandRunner | None = None,
        platform: str | None = None,
    ) -> None:
        self.task_name = task_name
        self.exe_path = exe_path
        self._runner = runner or default_command_runner
        self._platform = platform if platform is not None else sys.platform
        self._has_custom_runner = runner is not None

    @property
    def supported(self) -> bool:
        return self._platform == "win32" or self._has_custom_runner

    def build_create_command(self) -> list[str]:
        """Build the exact ``schtasks /Create`` argument list (no admin)."""
        return [
            "schtasks",
            "/Create",
            "/TN",
            self.task_name,
            "/TR",
            resolve_executable_command(self.exe_path),
            "/SC",
            "ONLOGON",
            "/RL",
            "LIMITED",
            "/F",
        ]

    def build_delete_command(self) -> list[str]:
        """Build the exact ``schtasks /Delete`` argument list."""
        return [
            "schtasks",
            "/Delete",
            "/TN",
            self.task_name,
            "/F",
        ]

    def build_query_command(self) -> list[str]:
        """Build the exact ``schtasks /Query`` argument list."""
        return [
            "schtasks",
            "/Query",
            "/TN",
            self.task_name,
        ]

    def is_enabled(self) -> bool:
        """Return True if the per-user autostart task currently exists."""
        if not self.supported:
            return False
        res = self._runner(self.build_query_command())
        return res.returncode == 0

    def enable(self) -> tuple[bool, str]:
        """Create or update the per-user ONLOGON task with LIMITED privileges."""
        if not self.supported:
            return False, "Autostart is only available on Windows."
        res = self._runner(self.build_create_command())
        if res.returncode == 0:
            return True, "Night-Shift will start on Windows sign-in (per-user task)."
        detail = (res.stderr or res.stdout).strip() or "Task Scheduler returned an error."
        return False, f"Could not enable autostart: {detail}"

    def disable(self) -> tuple[bool, str]:
        """Remove the per-user ONLOGON task if present."""
        if not self.supported:
            return True, "Autostart is off."
        res = self._runner(self.build_delete_command())
        if res.returncode == 0:
            return True, "Autostart turned off."
        combined = f"{res.stdout} {res.stderr}".lower()
        if "cannot find the file" in combined or "cannot find" in combined or "does not exist" in combined:
            return True, "Autostart is already off."
        detail = (res.stderr or res.stdout).strip() or "Task Scheduler returned an error."
        return False, f"Could not disable autostart: {detail}"

    def set_enabled(self, enabled: bool) -> tuple[bool, str]:
        """Enable or disable per-user autostart."""
        return self.enable() if enabled else self.disable()
