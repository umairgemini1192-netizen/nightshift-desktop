"""Post-merge build watcher, artifact download, SHA256SUMS, and GitHub release publisher (BLUEPRINT 5)."""

from __future__ import annotations

import fnmatch
import hashlib
import shutil
from pathlib import Path
from typing import Any

from nightshift.engine.vcs import GitGhClient


def compute_sha256(file_path: Path) -> str:
    """Compute hex-encoded SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class ReleaseManager:
    """Orchestrates post-merge artifact download, verification, and release creation."""

    def __init__(self, vcs: GitGhClient) -> None:
        self.vcs = vcs

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
    ) -> tuple[str, list[Path]]:
        """Watch post-merge build, download artifacts, compute SHA256, create release, copy to output_dir.

        Returns (tag, list_of_published_files).
        """
        # 1. Watch main build workflow
        if run_id is None:
            ok, msg, run_id = self.vcs.watch_workflow_run(repo, branch="main", workflow=workflow_name, timeout_seconds=3600, commit_sha=merge_sha)
            if not ok or run_id is None:
                raise RuntimeError(f"Main workflow '{workflow_name}' failed or not found: {msg}")

        # 2. Download artifacts
        work_tmp = temp_dir or (output_dir / "temp_download")
        if work_tmp.exists():
            shutil.rmtree(work_tmp, ignore_errors=True)
        work_tmp.mkdir(parents=True, exist_ok=True)

        downloaded_files = self.vcs.download_run_artifact(repo, run_id, work_tmp)
        matching_artifacts: list[Path] = []
        for f in downloaded_files:
            if fnmatch.fnmatch(f.name, artifact_glob) or fnmatch.fnmatch(str(f), artifact_glob):
                matching_artifacts.append(f)

        if not matching_artifacts:
            # Fallback: if glob didn't match nested paths, check all files
            matching_artifacts = [f for f in work_tmp.rglob("*") if f.is_file() and fnmatch.fnmatch(f.name, artifact_glob)]

        if not matching_artifacts:
            raise RuntimeError(f"No artifact matching glob '{artifact_glob}' found in workflow run {run_id}")

        # 3. Create next release tag
        tag = self.vcs.next_preview_tag(repo, tag_prefix=tag_prefix)

        # 4. Generate SHA256SUMS.txt
        sums_path = work_tmp / "SHA256SUMS.txt"
        sums_lines: list[str] = []
        for art in matching_artifacts:
            sha = compute_sha256(art)
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
