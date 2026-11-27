"""rtk-oss + portable ripgrep (rg.exe) per-run runner wiring (BLUEPRINT 5g).

Uses ONLY the Apache-2.0 OSS build asset (rtk-oss-x86_64-pc-windows-msvc.zip),
never the non-OSS build (rtk-x86_64-pc-windows-msvc.zip). Bundles a pinned
portable rg.exe (SHA-256 verified) and prepends it to PATH for Gemini CLI,
Claude, and Codex runner processes only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from nightshift.optimizer.tool_manager import (
    FORBIDDEN_NON_OSS_RTK_ASSET,
    REQUIRED_OSS_RTK_ASSET,
    ToolManager,
)

RTK_SUPPORTED_RUNNERS = frozenset({"gemini", "claude", "codex"})


@dataclass(frozen=True)
class RtkEnvResult:
    """Per-run environment prepared for rtk-oss and portable ripgrep."""

    enabled: bool
    env: dict[str, str] = field(default_factory=dict)
    warning: str = ""


def build_rtk_runner_env(
    tool_manager: ToolManager,
    runner_name: str,
    *,
    base_path: str | None = None,
) -> RtkEnvResult:
    """Prepare process-only environment for rtk-oss + rg.exe for supported runners.

    Returns enabled=False with a plain-English warning if:
    - runner is not one of gemini / claude / codex
    - no Windows binary is available for the pinned version
    - the lockfile points to the forbidden non-OSS asset
    - rtk-oss or ripgrep SHA-256 verification fails
    """
    normalized_runner = runner_name.strip().lower()
    if normalized_runner not in RTK_SUPPORTED_RUNNERS:
        return RtkEnvResult(
            enabled=False,
            env={},
            warning=f"rtk-oss is only wired for Gemini CLI, Claude, and Codex (skipped for {runner_name}).",
        )

    spec = tool_manager.get_spec("rtk-oss")
    if spec is None:
        return RtkEnvResult(
            enabled=False,
            env={},
            warning="rtk-oss is not listed in tools.lock.json and stays disabled.",
        )
    if not spec.windows_binary_available:
        return RtkEnvResult(
            enabled=False,
            env={},
            warning=f"rtk-oss {spec.version} is disabled because no Windows binary exists for this pinned version.",
        )
    if FORBIDDEN_NON_OSS_RTK_ASSET in spec.artifact or FORBIDDEN_NON_OSS_RTK_ASSET in spec.url:
        return RtkEnvResult(
            enabled=False,
            env={},
            warning=(
                f"rtk-oss is disabled because only the Apache-2.0 OSS asset ({REQUIRED_OSS_RTK_ASSET}) "
                "is permitted."
            ),
        )

    rtk_ver = tool_manager.verify_installed("rtk-oss")
    if not rtk_ver.ok or rtk_ver.executable_path is None:
        return RtkEnvResult(
            enabled=False,
            env={},
            warning=rtk_ver.warning or "rtk-oss binary failed verification and stays disabled.",
        )

    rg_ver = tool_manager.verify_installed("ripgrep")
    if not rg_ver.ok or rg_ver.executable_path is None:
        return RtkEnvResult(
            enabled=False,
            env={},
            warning=rg_ver.warning or "Portable ripgrep (rg.exe) failed verification; rtk-oss stays disabled.",
        )

    rg_dir = str(rg_ver.executable_path.parent)
    rtk_dir = str(rtk_ver.executable_path.parent)
    current_path = base_path if base_path is not None else os.environ.get("PATH", "")
    path_parts = [rg_dir, rtk_dir]
    if current_path:
        path_parts.append(current_path)
    scoped_path = os.pathsep.join(path_parts)

    env = tool_manager.build_process_env(
        {
            "PATH": scoped_path,
            "RTK_BIN": str(rtk_ver.executable_path),
            "RTK_RIPGREP_BIN": str(rg_ver.executable_path),
            "RTK_ENABLED": "1",
        }
    )
    return RtkEnvResult(enabled=True, env=env, warning="")
