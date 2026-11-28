"""Post-merge build watcher, artifact download, SHA256SUMS, GitHub release publisher,
mandatory post-release launch/smoke test, and one-click rollback (BLUEPRINT 5, AGENT_PIPELINE §9.3).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import shlex
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from nightshift.engine.vcs import GitGhClient
from nightshift.runners.base import run_subprocess_with_timeout

_CHECKSUM_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
DESKTOP_SELFTEST_SMOKE = "nightshift-desktop-selftest"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_sha256(file_path: Path) -> str:
    """Compute hex-encoded SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``<sha256>  <filename>`` lines from ``SHA256SUMS.txt``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _CHECKSUM_LINE_RE.match(line)
        if m:
            out[m.group(2).strip()] = m.group(1).lower()
    return out


@dataclass(frozen=True)
class SmokeTestResult:
    """Result of mandatory post-release launch/smoke verification (Amendment H)."""

    passed: bool
    status: str  # "passed" | "failed" | "unavailable"
    detail: str
    command: str = ""
    verified_at: str | None = None


@dataclass(frozen=True)
class RollbackResult:
    """Result of one-click release rollback (Amendment H)."""

    ok: bool
    faulty_tag: str
    restored_tag: str | None = None
    restored_files: list[str] = field(default_factory=list)
    revert_pr_created: bool = False
    detail: str = ""


# Signature for injectable smoke runner (for offline deterministic tests):
# (command_str, artifact_path, tag, timeout_seconds) -> SmokeTestResult
SmokeRunnerFn = Callable[[str, Path, str, int], SmokeTestResult]


class ReleaseManager:
    """Orchestrates post-merge artifact download, verification, release creation, smoke test, and rollback."""

    def __init__(
        self,
        vcs: GitGhClient,
        *,
        smoke_runner: SmokeRunnerFn | None = None,
    ) -> None:
        self.vcs = vcs
        self._smoke_runner = smoke_runner

    def prepare_verified_artifacts(
        self,
        repo: str,
        run_id: int,
        work_tmp: Path,
        *,
        artifact_glob: str = "*.apk",
    ) -> tuple[list[Path], dict[str, str]]:
        """Download artifacts from a verified workflow run, filter by glob, and compute SHA-256 checksums."""
        if work_tmp.exists():
            shutil.rmtree(work_tmp, ignore_errors=True)
        work_tmp.mkdir(parents=True, exist_ok=True)

        downloaded_files = self.vcs.download_run_artifact(repo, run_id, work_tmp)
        effective_glob = artifact_glob.strip() if artifact_glob else "*"
        matching_artifacts: list[Path] = []
        for f in downloaded_files:
            if f.name == "SHA256SUMS.txt":
                continue
            if fnmatch.fnmatch(f.name, effective_glob) or fnmatch.fnmatch(str(f), effective_glob):
                matching_artifacts.append(f)

        if not matching_artifacts:
            # Fallback: if glob didn't match nested paths, check all files
            matching_artifacts = [
                f
                for f in work_tmp.rglob("*")
                if f.is_file() and f.name != "SHA256SUMS.txt" and fnmatch.fnmatch(f.name, effective_glob)
            ]

        if not matching_artifacts:
            raise RuntimeError(f"No artifact matching glob '{effective_glob}' found in workflow run {run_id}")

        checksums = {art.name: compute_sha256(art) for art in matching_artifacts}
        return matching_artifacts, checksums

    def publish_verified_release(
        self,
        repo: str,
        output_dir: Path,
        title_note: str,
        *,
        merge_sha: str,
        workflow_name: str = "Android build",
        artifact_glob: str = "*.apk",
        tag_prefix: str = "v1.0.0-preview.",
        temp_dir: Path | None = None,
        run_id: int | None = None,
        prepared_artifacts: list[Path] | None = None,
    ) -> tuple[str, list[Path]]:
        """Watch post-merge build, download artifacts, compute SHA256, create release, copy to output_dir.

        Returns (tag, list_of_published_files). Never overwrites an existing release tag or asset.
        """
        # 1. Watch main build workflow
        if run_id is None:
            ok, msg, run_id = self.vcs.watch_workflow_run(
                repo, branch="main", workflow=workflow_name, timeout_seconds=3600, commit_sha=merge_sha
            )
            if not ok or run_id is None:
                raise RuntimeError(f"Main workflow '{workflow_name}' failed or not found: {msg}")

        # 2. Download artifacts (or reuse already-prepared artifacts from release reconciliation)
        work_tmp = temp_dir or (output_dir / "temp_download")
        if prepared_artifacts is not None:
            work_tmp.mkdir(parents=True, exist_ok=True)
            matching_artifacts = list(prepared_artifacts)
            if not matching_artifacts:
                raise RuntimeError(f"No artifact matching glob '{artifact_glob}' found in workflow run {run_id}")
            checksums = {art.name: compute_sha256(art) for art in matching_artifacts}
        else:
            matching_artifacts, checksums = self.prepare_verified_artifacts(
                repo,
                run_id,
                work_tmp,
                artifact_glob=artifact_glob,
            )

        # 3. Create next release tag (never overwrite an existing tag)
        tag = self.vcs.next_preview_tag(repo, tag_prefix=tag_prefix)

        # 4. Generate SHA256SUMS.txt
        sums_path = work_tmp / "SHA256SUMS.txt"
        sums_lines: list[str] = []
        for art in matching_artifacts:
            sha = checksums[art.name]
            sums_lines.append(f"{sha}  {art.name}")
        sums_path.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

        # 5. Create GitHub Release (embed merge_sha marker for durable reconciliation, AGENT_PIPELINE §4)
        release_files = matching_artifacts + [sums_path]
        title = f"{repo.split('/')[-1]} {tag}"
        notes = (
            f"Continuous Auto-Build: {title_note}\n"
            f"Main CI run: {run_id}\n"
            f"<!-- nightshift-merge-sha: {merge_sha} -->\n\n"
            f"Checksums:\n```\n" + "\n".join(sums_lines) + "\n```"
        )
        rel_ok, rel_msg = self.vcs.create_github_release(repo, tag, title, notes, release_files, prerelease=True)
        if not rel_ok:
            raise RuntimeError(f"GitHub release creation failed: {rel_msg}")

        # 6. Copy artifacts and checksums to project output directory
        output_dir.mkdir(parents=True, exist_ok=True)
        copied_files: list[Path] = []
        for art in matching_artifacts:
            dest_art = output_dir / art.name
            shutil.copy2(art, dest_art)
            copied_files.append(dest_art)

        dest_sums = output_dir / f"{tag}-SHA256SUMS.txt"
        shutil.copy2(sums_path, dest_sums)
        copied_files.append(dest_sums)

        # Cleanup temp dir if created inside output_dir
        if temp_dir is None:
            shutil.rmtree(work_tmp, ignore_errors=True)

        return tag, copied_files

    # ------------------------------------------------------------------
    # Mandatory post-release launch / smoke test (Amendment H / §9.3)
    # ------------------------------------------------------------------

    def run_launch_smoke_test(
        self,
        *,
        artifact_path: Path,
        tag: str,
        smoke_command: str,
        timeout_seconds: int = 120,
    ) -> SmokeTestResult:
        """Execute the configured runtime launch/smoke test on the published artifact.

        Fail-closed: returns ``status='unavailable'`` if ``smoke_command`` is empty or
        the required executable/emulator is missing; returns ``status='failed'`` if the
        launch/smoke check fails.
        """
        cmd_spec = (smoke_command or "").strip()
        if not cmd_spec:
            return SmokeTestResult(
                passed=False,
                status="unavailable",
                detail="No post-release launch/smoke test procedure is configured for this project.",
                command="",
            )

        if not artifact_path.exists() or not artifact_path.is_file():
            return SmokeTestResult(
                passed=False,
                status="unavailable",
                detail=f"Release artifact '{artifact_path}' is missing and cannot be smoke-tested.",
                command=cmd_spec,
            )

        if self._smoke_runner is not None:
            return self._smoke_runner(cmd_spec, artifact_path, tag, timeout_seconds)

        # Built-in Night-Shift Desktop zip smoke test (AGENT_PIPELINE §9.3)
        if cmd_spec == DESKTOP_SELFTEST_SMOKE:
            return self._run_desktop_zip_selftest(artifact_path, timeout_seconds=timeout_seconds)

        # Custom configured launch/smoke command (parsed as argv, never shell=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="nightshift-smoke-"))
        try:
            output_file = tmp_dir / "smoke-result.txt"
            extract_dir = tmp_dir / "extracted"
            if artifact_path.suffix.lower() == ".zip":
                extract_dir.mkdir(parents=True, exist_ok=True)
                try:
                    with zipfile.ZipFile(artifact_path, "r") as zf:
                        zf.extractall(extract_dir)
                except Exception as exc:
                    return SmokeTestResult(
                        passed=False,
                        status="failed",
                        detail=f"Could not extract release archive '{artifact_path.name}': {exc}",
                        command=cmd_spec,
                    )

            try:
                raw_argv = shlex.split(cmd_spec, posix=False)
            except ValueError as exc:
                return SmokeTestResult(
                    passed=False,
                    status="unavailable",
                    detail=f"Invalid smoke_command syntax: {exc}",
                    command=cmd_spec,
                )
            if not raw_argv:
                return SmokeTestResult(
                    passed=False,
                    status="unavailable",
                    detail="Empty smoke_command after parsing.",
                    command=cmd_spec,
                )

            substitutions = {
                "{artifact}": str(artifact_path),
                "{extract_dir}": str(extract_dir),
                "{output_file}": str(output_file),
                "{tag}": tag,
            }
            argv: list[str] = []
            for tok in raw_argv:
                clean = tok.strip('"') if len(tok) >= 2 and tok[0] == tok[-1] == '"' else tok
                for k, v in substitutions.items():
                    clean = clean.replace(k, v)
                argv.append(clean)

            rc, out, err, timed_out = run_subprocess_with_timeout(
                argv,
                cwd=tmp_dir,
                timeout_seconds=timeout_seconds,
            )
            if rc == -1 and "Executable not found" in (err or ""):
                return SmokeTestResult(
                    passed=False,
                    status="unavailable",
                    detail=f"Smoke test runner executable not found: {err}",
                    command=cmd_spec,
                )
            if timed_out:
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail=f"Post-release smoke test timed out after {timeout_seconds}s.",
                    command=cmd_spec,
                )
            if rc != 0:
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail=f"Post-release smoke test exited with code {rc}: {(err or out)[:300]}",
                    command=cmd_spec,
                )
            return SmokeTestResult(
                passed=True,
                status="passed",
                detail=f"Post-release smoke test passed: {(out or 'OK')[:200]}",
                command=cmd_spec,
                verified_at=_utc_now(),
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _run_desktop_zip_selftest(self, zip_path: Path, *, timeout_seconds: int = 120) -> SmokeTestResult:
        """Extract NightShift-windows.zip to a clean temp dir and run NightShift.exe --self-test."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="nightshift-desktop-smoke-"))
        try:
            extract_dir = tmp_dir / "app"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
            except Exception as exc:
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail=f"Failed to extract {zip_path.name}: {exc}",
                    command=DESKTOP_SELFTEST_SMOKE,
                )

            exe_candidates = list(extract_dir.rglob("NightShift.exe"))
            if not exe_candidates:
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail=f"NightShift.exe not found inside extracted {zip_path.name}.",
                    command=DESKTOP_SELFTEST_SMOKE,
                )
            exe_path = exe_candidates[0]
            result_file = tmp_dir / "selftest-result.txt"
            rc, out, err, timed_out = run_subprocess_with_timeout(
                [str(exe_path), "--self-test", "--self-test-output", str(result_file)],
                cwd=exe_path.parent,
                timeout_seconds=timeout_seconds,
            )
            if timed_out:
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail=f"NightShift.exe --self-test timed out after {timeout_seconds}s.",
                    command=DESKTOP_SELFTEST_SMOKE,
                )
            if rc != 0:
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail=f"NightShift.exe --self-test exited with code {rc}: {(err or out)[:200]}",
                    command=DESKTOP_SELFTEST_SMOKE,
                )
            if not result_file.exists():
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail="NightShift.exe --self-test wrote no result file.",
                    command=DESKTOP_SELFTEST_SMOKE,
                )
            trimmed = result_file.read_text(encoding="utf-8", errors="replace").strip()
            if trimmed != "OK":
                return SmokeTestResult(
                    passed=False,
                    status="failed",
                    detail=f"NightShift.exe --self-test reported unexpected result: '{trimmed}'.",
                    command=DESKTOP_SELFTEST_SMOKE,
                )
            return SmokeTestResult(
                passed=True,
                status="passed",
                detail="NightShift.exe --self-test passed with OK.",
                command=DESKTOP_SELFTEST_SMOKE,
                verified_at=_utc_now(),
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def verify_and_record_release(
        self,
        *,
        store: Any,
        repo: str,
        tag: str,
        merge_sha: str,
        artifact_path: Path,
        smoke_command: str,
        task_id: str | None = None,
        contract_sha256: str | None = None,
        timeout_seconds: int = 120,
    ) -> SmokeTestResult:
        """Run mandatory post-release smoke test and persist ``release_verifications`` + ``evidence_records``."""
        artifact_sha = compute_sha256(artifact_path) if artifact_path.exists() else ""
        prev_good = store.get_latest_good_release(repo, exclude_tag=tag)
        previous_good_tag = str(prev_good["tag"]) if prev_good else None

        res = self.run_launch_smoke_test(
            artifact_path=artifact_path,
            tag=tag,
            smoke_command=smoke_command,
            timeout_seconds=timeout_seconds,
        )

        db_status = "passed" if res.passed else ("failed" if res.status == "failed" else "pending")
        store.record_release_verification(
            tag=tag,
            task_id=task_id,
            repo=repo,
            merge_sha=merge_sha,
            previous_good_tag=previous_good_tag,
            artifact_sha256=artifact_sha,
            smoke_command=smoke_command or "",
            smoke_status=db_status,
            smoke_detail=res.detail,
            verified_at=res.verified_at if res.passed else None,
        )

        if task_id and contract_sha256:
            store.record_evidence(
                task_id=task_id,
                contract_sha256=contract_sha256,
                commit_sha=merge_sha,
                kind="smoke_test",
                source=f"release:{tag}",
                artifact_paths=[str(artifact_path.name)],
                metadata={
                    "tag": tag,
                    "smoke_command": smoke_command,
                    "smoke_status": res.status,
                    "artifact_sha256": artifact_sha,
                    "previous_good_tag": previous_good_tag,
                },
                verified=res.passed,
                rejection_reason="" if res.passed else f"smoke_test_{res.status}",
            )

        return res

    # ------------------------------------------------------------------
    # One-click rollback (Amendment H / §9.3)
    # ------------------------------------------------------------------

    def rollback_release(
        self,
        *,
        store: Any,
        repo: str,
        faulty_tag: str,
        output_dir: Path,
        open_revert_pr: bool = False,
        local_repo: Path | None = None,
    ) -> RollbackResult:
        """Roll back a faulty release to ``previous_good_tag`` without deleting history or overwriting tags.

        Steps (AGENT_PIPELINE §9.3):
        1. Look up ``faulty_tag`` and its ``previous_good_tag`` in ``release_verifications``.
        2. Append a superseded warning to ``faulty_tag``'s GitHub release notes.
        3. Restore ``previous_good_tag``'s verified artifact(s) + ``SHA256SUMS.txt`` into ``output_dir``
           and verify SHA-256 checksums.
        4. Optionally open a revert PR on ``main`` for the faulty ``merge_sha``.
        5. Mark ``faulty_tag`` as ``rolled_back`` in ``release_verifications``.
        """
        faulty = store.get_release_verification(faulty_tag)
        if faulty is None:
            return RollbackResult(
                ok=False,
                faulty_tag=faulty_tag,
                detail=f"Release verification record not found for tag '{faulty_tag}'.",
            )

        prev_tag = str(faulty.get("previous_good_tag") or "").strip()
        if not prev_tag:
            prev_row = store.get_latest_good_release(repo, exclude_tag=faulty_tag)
            prev_tag = str(prev_row["tag"]) if prev_row else ""
        if not prev_tag:
            return RollbackResult(
                ok=False,
                faulty_tag=faulty_tag,
                detail=f"No previous known-good release recorded for {repo}; cannot roll back '{faulty_tag}'.",
            )

        prev_rec = store.get_release_verification(prev_tag)
        expected_prev_sha = str((prev_rec or {}).get("artifact_sha256") or "").lower()

        # 1. Append superseded warning to faulty release notes (never delete tags/history)
        warning_text = (
            f"> **SUPERSEDED / ROLLED BACK**: Release `{faulty_tag}` failed post-release verification "
            f"and was rolled back to `{prev_tag}` at {_utc_now()}."
        )
        warn_ok, warn_msg = self.vcs.append_release_warning(repo, faulty_tag, warning_text)
        if not warn_ok:
            return RollbackResult(
                ok=False,
                faulty_tag=faulty_tag,
                restored_tag=prev_tag,
                detail=f"Could not append superseded warning to {faulty_tag}: {warn_msg}",
            )

        # 2. Download & verify previous_good_tag assets + SHA256SUMS.txt
        tmp_restore = Path(tempfile.mkdtemp(prefix="nightshift-rollback-"))
        restored_files: list[str] = []
        try:
            downloaded = self.vcs.download_release_assets(repo, prev_tag, tmp_restore)
            sums_file = next((p for p in downloaded if p.name == "SHA256SUMS.txt"), None)
            if sums_file is None:
                return RollbackResult(
                    ok=False,
                    faulty_tag=faulty_tag,
                    restored_tag=prev_tag,
                    detail=f"Previous good release '{prev_tag}' is missing SHA256SUMS.txt; refusing unverified restore.",
                )
            recorded_sums = parse_sha256sums(sums_file.read_text(encoding="utf-8", errors="replace"))
            artifacts = [p for p in downloaded if p.name != "SHA256SUMS.txt"]
            if not artifacts:
                return RollbackResult(
                    ok=False,
                    faulty_tag=faulty_tag,
                    restored_tag=prev_tag,
                    detail=f"Previous good release '{prev_tag}' has no downloadable artifacts.",
                )

            matched_prev_sha = False
            for art in artifacts:
                actual_sha = compute_sha256(art).lower()
                exp_sum = recorded_sums.get(art.name)
                if not exp_sum or actual_sha != exp_sum:
                    return RollbackResult(
                        ok=False,
                        faulty_tag=faulty_tag,
                        restored_tag=prev_tag,
                        detail=f"Checksum mismatch restoring '{art.name}' from '{prev_tag}'.",
                    )
                if expected_prev_sha and actual_sha == expected_prev_sha:
                    matched_prev_sha = True

            if expected_prev_sha and not matched_prev_sha:
                return RollbackResult(
                    ok=False,
                    faulty_tag=faulty_tag,
                    restored_tag=prev_tag,
                    detail=f"Downloaded assets from '{prev_tag}' do not match recorded artifact_sha256.",
                )

            output_dir.mkdir(parents=True, exist_ok=True)
            for art in artifacts:
                dest = output_dir / art.name
                shutil.copy2(art, dest)
                restored_files.append(dest.name)
            dest_sums = output_dir / f"{prev_tag}-SHA256SUMS.txt"
            shutil.copy2(sums_file, dest_sums)
            restored_files.append(dest_sums.name)
        finally:
            shutil.rmtree(tmp_restore, ignore_errors=True)

        # 3. Optionally open a revert PR on main for the faulty merge_sha
        revert_created = False
        if open_revert_pr and local_repo is not None and faulty.get("merge_sha"):
            rev_ok, rev_msg = self.vcs.revert_via_pr(repo, local_repo, str(faulty["merge_sha"]))
            if not rev_ok:
                return RollbackResult(
                    ok=False,
                    faulty_tag=faulty_tag,
                    restored_tag=prev_tag,
                    restored_files=restored_files,
                    revert_pr_created=False,
                    detail=f"Restored {prev_tag}, but revert PR failed: {rev_msg}",
                )
            revert_created = True

        # 4. Update release_verifications record
        store.update_release_verification(
            faulty_tag,
            smoke_status="rolled_back",
            rolled_back_at=_utc_now(),
        )

        return RollbackResult(
            ok=True,
            faulty_tag=faulty_tag,
            restored_tag=prev_tag,
            restored_files=restored_files,
            revert_pr_created=revert_created,
            detail=f"Rolled back {faulty_tag} to verified release {prev_tag} ({len(restored_files)} files restored).",
        )
