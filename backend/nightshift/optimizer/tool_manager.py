"""Pinned local tool manager with SHA-256 verification and zero global writes (BLUEPRINT 5g)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.store import Store

TELEMETRY_OFF_ENV: dict[str, str] = {
    "DO_NOT_TRACK": "1",
    "NO_TELEMETRY": "1",
    "DISABLE_TELEMETRY": "1",
    "RTK_TELEMETRY_DISABLED": "1",
}

FORBIDDEN_NON_OSS_RTK_ASSET = "rtk-x86_64-pc-windows-msvc.zip"
REQUIRED_OSS_RTK_ASSET = "rtk-oss-x86_64-pc-windows-msvc.zip"
REQUIRED_AGY_FAST_COMMIT = "fe635e1"

DownloaderFn = Callable[[str, Path], Path]


def default_tools_root() -> Path:
    """Return %LOCALAPPDATA%/NightShift/tools (per-user, no admin)."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NightShift" / "tools"
    return Path.home() / ".local" / "share" / "NightShift" / "tools"


def resolve_lockfile_path(explicit: Path | None = None) -> Path:
    """Locate tools.lock.json in the repo root or PyInstaller bundle."""
    if explicit is not None:
        return explicit
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "tools.lock.json"
        if bundled.exists():
            return bundled
    # backend/nightshift/optimizer/tool_manager.py -> parents[3] is repo root
    repo_candidate = Path(__file__).resolve().parents[3] / "tools.lock.json"
    if repo_candidate.exists():
        return repo_candidate
    return Path.cwd() / "tools.lock.json"


def compute_sha256(path: Path) -> str:
    """Compute lowercase SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _default_downloader(url: str, dest: Path) -> Path:
    """Download a pinned artifact directly over HTTPS (never curl|sh or irm|iex)."""
    if not url.lower().startswith("https://"):
        raise ValueError(f"Refusing non-HTTPS tool download URL: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "NightShift-Desktop/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())
    return dest


@dataclass(frozen=True)
class ToolSpec:
    """Pinned tool entry loaded from tools.lock.json."""

    name: str
    display_name: str
    role: str
    version: str
    artifact: str
    url: str
    sha256: str
    executable: str
    flags: tuple[str, ...] = ()
    runners: tuple[str, ...] = ()
    enabled_by_default: bool = False
    auto_update: bool = False
    catalogue_only: bool = False
    isolation_gate_required: bool = False
    windows_binary_available: bool = True
    requires_tools: tuple[str, ...] = ()
    token_budget: int = 12000
    commit: str = ""
    license: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ToolSpec:
        return cls(
            name=name,
            display_name=str(data.get("display_name", name)),
            role=str(data.get("role", "")),
            version=str(data["version"]),
            artifact=str(data["artifact"]),
            url=str(data.get("url", "")),
            sha256=str(data["sha256"]).strip().lower(),
            executable=str(data.get("executable", name)),
            flags=tuple(str(f) for f in (data.get("flags") or [])),
            runners=tuple(str(r) for r in (data.get("runners") or [])),
            enabled_by_default=bool(data.get("enabled_by_default", False)),
            auto_update=bool(data.get("auto_update", False)),
            catalogue_only=bool(data.get("catalogue_only", False)),
            isolation_gate_required=bool(data.get("isolation_gate_required", False)),
            windows_binary_available=bool(data.get("windows_binary_available", True)),
            requires_tools=tuple(str(r) for r in (data.get("requires_tools") or [])),
            token_budget=int(data.get("token_budget", 12000)),
            commit=str(data.get("commit", "")),
            license=str(data.get("license", "")),
        )


@dataclass(frozen=True)
class ToolVerificationResult:
    """Result of verifying or installing a pinned tool."""

    tool: str
    ok: bool
    installed_path: Path | None = None
    executable_path: Path | None = None
    expected_sha256: str = ""
    actual_sha256: str = ""
    warning: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ToolManager:
    """Manages pinned, SHA-256 verified, per-user local optimizer tools."""

    def __init__(
        self,
        *,
        store: Store | None = None,
        tools_root: Path | None = None,
        lockfile_path: Path | None = None,
        downloader: DownloaderFn | None = None,
    ) -> None:
        self._store = store
        self._tools_root = Path(tools_root) if tools_root is not None else default_tools_root()
        self._lockfile_path = resolve_lockfile_path(lockfile_path)
        self._downloader: DownloaderFn = downloader or _default_downloader
        self._specs: dict[str, ToolSpec] = self._load_lockfile(self._lockfile_path)

    @staticmethod
    def _load_lockfile(path: Path) -> dict[str, ToolSpec]:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        tools_raw = raw.get("tools") or {}
        specs: dict[str, ToolSpec] = {}
        for name, entry in tools_raw.items():
            if isinstance(entry, dict):
                specs[name] = ToolSpec.from_dict(name, entry)
        return specs

    @property
    def tools_root(self) -> Path:
        return self._tools_root

    @property
    def lockfile_path(self) -> Path:
        return self._lockfile_path

    def list_specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def get_spec(self, tool_name: str) -> ToolSpec | None:
        return self._specs.get(tool_name)

    def tool_dir(self, tool_name: str) -> Path:
        spec = self._specs[tool_name]
        return self._tools_root / spec.name / spec.version

    def artifact_path(self, tool_name: str) -> Path:
        spec = self._specs[tool_name]
        return self.tool_dir(tool_name) / spec.artifact

    def executable_path(self, tool_name: str) -> Path:
        spec = self._specs[tool_name]
        return self.tool_dir(tool_name) / spec.executable

    def build_process_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        """Return a process-scoped env with telemetry disabled and no global mutation."""
        env = dict(TELEMETRY_OFF_ENV)
        if extra_env:
            env.update(extra_env)
        return env

    def validate_policy(self, spec: ToolSpec) -> str | None:
        """Validate BLUEPRINT 5g security and licensing constraints for a tool spec."""
        if spec.auto_update:
            return f"{spec.display_name} is disabled because auto-update must be turned off."
        if spec.catalogue_only:
            return f"{spec.display_name} is catalogue only and stays disabled until search is proven a top cost."
        if spec.name == "rtk-oss":
            if not spec.windows_binary_available:
                return f"{spec.display_name} is disabled because no portable Windows binary is available for version {spec.version}."
            if FORBIDDEN_NON_OSS_RTK_ASSET in spec.artifact or FORBIDDEN_NON_OSS_RTK_ASSET in spec.url:
                return (
                    f"{spec.display_name} is disabled because only the Apache-2.0 OSS build "
                    f"({REQUIRED_OSS_RTK_ASSET}) is allowed."
                )
            if REQUIRED_OSS_RTK_ASSET not in spec.artifact:
                return (
                    f"{spec.display_name} is disabled because its artifact does not match "
                    f"{REQUIRED_OSS_RTK_ASSET}."
                )
        if spec.name == "agy-fast" and spec.commit and spec.commit != REQUIRED_AGY_FAST_COMMIT:
            return (
                f"{spec.display_name} is disabled because commit {spec.commit} does not match "
                f"the approved pinned commit {REQUIRED_AGY_FAST_COMMIT}."
            )
        return None

    def _record_state(
        self,
        tool_name: str,
        *,
        enabled: bool | None = None,
        installed_version: str | None = None,
        sha256_verified: bool | None = None,
        warning: str | None = None,
    ) -> None:
        if self._store is None:
            return
        self._store.upsert_token_tool_state(
            tool_name,
            enabled=enabled,
            installed_version=installed_version,
            sha256_verified=sha256_verified,
            warning=warning,
        )

    def ensure_installed(self, tool_name: str, *, download_if_missing: bool = True) -> ToolVerificationResult:
        """Download a pinned artifact once (if needed) and verify its SHA-256 before use.

        Refuses to run on SHA-256 mismatch, disables the tool, and returns a plain-English warning.
        """
        spec = self.get_spec(tool_name)
        if spec is None:
            return ToolVerificationResult(
                tool=tool_name,
                ok=False,
                warning=f"Tool '{tool_name}' is not listed in tools.lock.json and cannot run.",
            )

        policy_error = self.validate_policy(spec)
        if policy_error:
            self._record_state(tool_name, enabled=False, sha256_verified=False, warning=policy_error)
            return ToolVerificationResult(
                tool=tool_name,
                ok=False,
                expected_sha256=spec.sha256,
                warning=policy_error,
            )

        tdir = self.tool_dir(tool_name)
        artifact = self.artifact_path(tool_name)

        if not artifact.exists():
            if not download_if_missing:
                msg = f"{spec.display_name} {spec.version} is not downloaded yet."
                self._record_state(tool_name, sha256_verified=False, warning=msg)
                return ToolVerificationResult(
                    tool=tool_name,
                    ok=False,
                    expected_sha256=spec.sha256,
                    warning=msg,
                )
            tdir.mkdir(parents=True, exist_ok=True)
            try:
                self._downloader(spec.url, artifact)
            except Exception as exc:
                msg = f"Could not download {spec.display_name} {spec.version}: {exc}"
                self._record_state(tool_name, enabled=False, sha256_verified=False, warning=msg)
                return ToolVerificationResult(
                    tool=tool_name,
                    ok=False,
                    expected_sha256=spec.sha256,
                    warning=msg,
                )

        return self.verify_installed(tool_name)

    def verify_installed(self, tool_name: str) -> ToolVerificationResult:
        """Verify the SHA-256 of the local artifact against tools.lock.json."""
        spec = self.get_spec(tool_name)
        if spec is None:
            return ToolVerificationResult(
                tool=tool_name,
                ok=False,
                warning=f"Tool '{tool_name}' is not listed in tools.lock.json.",
            )

        policy_error = self.validate_policy(spec)
        if policy_error:
            self._record_state(tool_name, enabled=False, sha256_verified=False, warning=policy_error)
            return ToolVerificationResult(
                tool=tool_name,
                ok=False,
                expected_sha256=spec.sha256,
                warning=policy_error,
            )

        artifact = self.artifact_path(tool_name)
        if not artifact.exists():
            msg = f"{spec.display_name} {spec.version} is not installed in the local tools folder."
            self._record_state(tool_name, sha256_verified=False, warning="")
            return ToolVerificationResult(
                tool=tool_name,
                ok=False,
                expected_sha256=spec.sha256,
                warning=msg,
            )

        actual = compute_sha256(artifact)
        if actual != spec.sha256:
            warning = (
                f"{spec.display_name} {spec.version} was disabled because its SHA-256 checksum "
                f"did not match tools.lock.json. Re-download or check the pinned lockfile before enabling."
            )
            self._record_state(
                tool_name,
                enabled=False,
                installed_version=spec.version,
                sha256_verified=False,
                warning=warning,
            )
            return ToolVerificationResult(
                tool=tool_name,
                ok=False,
                installed_path=artifact,
                expected_sha256=spec.sha256,
                actual_sha256=actual,
                warning=warning,
            )

        # Extract zip portable binaries inside the versioned tool dir only (never outside).
        tdir = self.tool_dir(tool_name)
        exe_path = self.executable_path(tool_name)
        if artifact.suffix.lower() == ".zip" and not exe_path.exists():
            try:
                self._safe_extract_zip(artifact, tdir)
            except Exception as exc:
                warning = f"Could not unpack {spec.display_name} {spec.version}: {exc}"
                self._record_state(tool_name, enabled=False, sha256_verified=False, warning=warning)
                return ToolVerificationResult(
                    tool=tool_name,
                    ok=False,
                    installed_path=artifact,
                    expected_sha256=spec.sha256,
                    actual_sha256=actual,
                    warning=warning,
                )

        # Locate executable if extracted into a nested folder inside tdir.
        resolved_exe = exe_path
        if not resolved_exe.exists():
            matches = list(tdir.rglob(spec.executable))
            if matches:
                resolved_exe = matches[0]

        self._record_state(
            tool_name,
            installed_version=spec.version,
            sha256_verified=True,
            warning="",
        )
        return ToolVerificationResult(
            tool=tool_name,
            ok=True,
            installed_path=artifact,
            executable_path=resolved_exe,
            expected_sha256=spec.sha256,
            actual_sha256=actual,
            warning="",
        )

    @staticmethod
    def _safe_extract_zip(archive: Path, target_dir: Path) -> None:
        """Extract a zip strictly inside target_dir, preventing zip-slip path traversal."""
        resolved_root = target_dir.resolve()
        with zipfile.ZipFile(archive, "r") as zf:
            for member in zf.infolist():
                dest = (target_dir / member.filename).resolve()
                if not str(dest).startswith(str(resolved_root)):
                    raise ValueError(f"Unsafe zip entry path: {member.filename}")
            zf.extractall(target_dir)
