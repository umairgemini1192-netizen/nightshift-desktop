"""agy-fast isolation gate and per-run wiring (BLUEPRINT 5g).

agy-fast is ONLY enabled if an automated probe proves that Antigravity (agy)
can load the plugin per run via a session-only settings flag (--settings) or a
project-local folder WITHOUT modifying ~/.gemini/config (verified by hashing
all files in ~/.gemini/config before and after the probe).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.optimizer.tool_manager import REQUIRED_AGY_FAST_COMMIT, ToolManager
from nightshift.runners.base import run_subprocess_with_timeout

CommandExecutorFn = Callable[[list[str], Path | None, dict[str, str], int], tuple[int, str, str, bool]]


def _default_exec(
    cmd: list[str],
    cwd: Path | None,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, str, str, bool]:
    return run_subprocess_with_timeout(cmd, cwd=cwd, timeout_seconds=timeout_seconds, env=env)


def snapshot_directory_hashes(directory: Path) -> dict[str, str]:
    """Return a sorted map of relative_path -> sha256 for every file under directory."""
    if not directory.exists():
        return {}
    hashes: dict[str, str] = {}
    for file_path in sorted(directory.rglob("*")):
        if file_path.is_file():
            rel = file_path.relative_to(directory).as_posix()
            h = hashlib.sha256(file_path.read_bytes()).hexdigest()
            hashes[rel] = h
    return hashes


@dataclass(frozen=True)
class IsolationProbeResult:
    """Result of probing whether agy supports per-run agy-fast without global config writes."""

    passed: bool
    method: str = "none"  # "settings_flag" | "project_local" | "none"
    reason: str = ""
    global_config_modified: bool = False
    candidates_tried: list[str] = field(default_factory=list)


def probe_agy_isolation(
    *,
    tool_manager: ToolManager,
    home_dir: Path | None = None,
    agy_binary: str = "agy",
    executor: CommandExecutorFn | None = None,
    timeout_seconds: int = 30,
) -> IsolationProbeResult:
    """Test in a throwaway temp project whether agy loads agy-fast without touching ~/.gemini/config."""
    spec = tool_manager.get_spec("agy-fast")
    if spec is None:
        return IsolationProbeResult(
            passed=False,
            reason="agy-fast is not listed in tools.lock.json and stays disabled.",
        )
    if spec.auto_update:
        return IsolationProbeResult(
            passed=False,
            reason="agy-fast auto-update must be off before enabling.",
        )
    if spec.commit and spec.commit != REQUIRED_AGY_FAST_COMMIT:
        return IsolationProbeResult(
            passed=False,
            reason=f"agy-fast commit {spec.commit} does not match approved commit {REQUIRED_AGY_FAST_COMMIT}.",
        )

    exec_fn = executor or _default_exec
    resolved_home = Path(home_dir) if home_dir is not None else Path.home()
    global_config_dir = resolved_home / ".gemini" / "config"
    plugin_path = str(tool_manager.executable_path("agy-fast"))
    base_env = tool_manager.build_process_env({"AGY_FAST_AUTO_UPDATE": "0"})
    tried: list[str] = []
    any_global_mutation = False

    with tempfile.TemporaryDirectory(prefix="nightshift-agy-probe-") as tmp:
        project_dir = Path(tmp)

        # Candidate 1: session-only settings flag (agy --settings <file>)
        tried.append("settings_flag")
        settings_file = project_dir / ".nightshift_agy_settings.json"
        settings_payload = {
            "plugins": [
                {
                    "name": "agy-fast",
                    "path": plugin_path,
                    "commit": spec.commit or REQUIRED_AGY_FAST_COMMIT,
                    "autoUpdate": False,
                }
            ]
        }
        settings_file.write_text(json.dumps(settings_payload), encoding="utf-8")

        before = snapshot_directory_hashes(global_config_dir)
        rc, out, err, timed_out = exec_fn(
            [
                agy_binary,
                "--settings",
                str(settings_file),
                "-p",
                "Reply with AGY_FAST_LOADED if the agy-fast plugin is loaded.",
                "--output-format",
                "json",
            ],
            project_dir,
            base_env,
            timeout_seconds,
        )
        after = snapshot_directory_hashes(global_config_dir)
        mutated = before != after
        any_global_mutation = any_global_mutation or mutated

        if mutated:
            return IsolationProbeResult(
                passed=False,
                method="none",
                reason=(
                    "agy-fast stayed disabled because probing Antigravity modified ~/.gemini/config. "
                    "Night-Shift never edits your global IDE or CLI configuration."
                ),
                global_config_modified=True,
                candidates_tried=tried,
            )

        if rc == 0 and not timed_out and "AGY_FAST_LOADED" in f"{out}\n{err}":
            return IsolationProbeResult(
                passed=True,
                method="settings_flag",
                reason="Antigravity loaded agy-fast via --settings without modifying ~/.gemini/config.",
                global_config_modified=False,
                candidates_tried=tried,
            )

        # Candidate 2: project-local plugin folder (.gemini/plugins/agy-fast)
        tried.append("project_local")
        local_plugin_dir = project_dir / ".gemini" / "plugins" / "agy-fast"
        local_plugin_dir.mkdir(parents=True, exist_ok=True)
        (local_plugin_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "agy-fast",
                    "entry": plugin_path,
                    "commit": spec.commit or REQUIRED_AGY_FAST_COMMIT,
                    "autoUpdate": False,
                }
            ),
            encoding="utf-8",
        )

        before_local = snapshot_directory_hashes(global_config_dir)
        rc2, out2, err2, timed_out2 = exec_fn(
            [
                agy_binary,
                "-p",
                "Reply with AGY_FAST_LOADED if the project-local agy-fast plugin is loaded.",
                "--output-format",
                "json",
            ],
            project_dir,
            base_env,
            timeout_seconds,
        )
        after_local = snapshot_directory_hashes(global_config_dir)
        mutated_local = before_local != after_local
        any_global_mutation = any_global_mutation or mutated_local

        if not mutated_local and rc2 == 0 and not timed_out2 and "AGY_FAST_LOADED" in f"{out2}\n{err2}":
            return IsolationProbeResult(
                passed=True,
                method="project_local",
                reason="Antigravity loaded agy-fast from a project-local folder without modifying ~/.gemini/config.",
                global_config_modified=False,
                candidates_tried=tried,
            )

    if any_global_mutation:
        reason = (
            "agy-fast stayed disabled because probing Antigravity modified ~/.gemini/config. "
            "Night-Shift never edits your global IDE or CLI configuration."
        )
    else:
        reason = (
            "agy-fast stayed disabled because Antigravity could not load it per run via --settings "
            "or a project-local folder without global config edits. repomix still covers cold-start savings."
        )
    return IsolationProbeResult(
        passed=False,
        method="none",
        reason=reason,
        global_config_modified=any_global_mutation,
        candidates_tried=tried,
    )


def build_agy_fast_session_args(
    tool_manager: ToolManager,
    worktree_dir: Path,
    *,
    method: str,
) -> tuple[list[str], dict[str, str]]:
    """Build per-run CLI args and env for agy-fast when the isolation gate has passed."""
    spec = tool_manager.get_spec("agy-fast")
    if spec is None or spec.auto_update:
        return [], {}
    env = tool_manager.build_process_env({"AGY_FAST_AUTO_UPDATE": "0"})
    plugin_path = str(tool_manager.executable_path("agy-fast"))

    if method == "settings_flag":
        settings_path = worktree_dir / ".nightshift" / "agy_fast_settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(
                {
                    "plugins": [
                        {
                            "name": "agy-fast",
                            "path": plugin_path,
                            "commit": spec.commit or REQUIRED_AGY_FAST_COMMIT,
                            "autoUpdate": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return ["--settings", str(settings_path)], env

    if method == "project_local":
        local_dir = worktree_dir / ".nightshift" / "agy_fast_plugin"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "agy-fast",
                    "entry": plugin_path,
                    "commit": spec.commit or REQUIRED_AGY_FAST_COMMIT,
                    "autoUpdate": False,
                }
            ),
            encoding="utf-8",
        )
        return [], env

    return [], {}
