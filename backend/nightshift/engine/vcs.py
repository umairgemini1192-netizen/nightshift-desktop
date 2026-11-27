"""VCS and GitHub CLI abstraction layer for engine execution and offline testing."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from nightshift.runners.base import run_subprocess_with_timeout

if TYPE_CHECKING:
    from nightshift.engine.pipeline_contracts import DiffPathEntry


class GitGhClient(Protocol):
    """Protocol for all Git and GitHub CLI interactions."""

    def fetch_origin(self, local_repo: Path) -> tuple[int, str, str]: ...

    def remote_branch_exists(self, local_repo: Path, branch: str) -> bool: ...

    def remote_branch_sha(self, local_repo: Path, branch: str) -> str | None: ...

    def local_head_sha(self, worktree_dir: Path) -> str: ...

    def prepare_worktree(
        self,
        local_repo: Path,
        worktree_dir: Path,
        branch: str,
        base_branch: str = "main",
    ) -> Path: ...

    def remove_worktree(self, local_repo: Path, worktree_dir: Path) -> None: ...

    def find_open_pr(self, repo: str, branch: str) -> dict[str, Any] | None: ...

    def find_merged_pr(self, repo: str, branch: str) -> dict[str, Any] | None: ...

    def watch_pr_checks(
        self,
        repo: str,
        pr_number: int,
        worktree_dir: Path,
        timeout_seconds: int = 3600,
    ) -> tuple[bool, str]: ...

    def get_failed_ci_log(self, repo: str, branch: str) -> str: ...

    def merge_pr(
        self,
        repo: str,
        pr_number: int,
        local_repo: Path,
        strategy: str = "--merge",
    ) -> tuple[bool, str]: ...

    def create_checkpoint_commit_and_push(
        self,
        worktree_dir: Path,
        branch: str,
        message: str,
    ) -> str | None: ...

    def merge_main_into_branch(self, worktree_dir: Path) -> tuple[bool, str]: ...

    def revert_via_pr(self, repo: str, local_repo: Path, commit_sha: str) -> tuple[bool, str]: ...

    def check_branch_protection(self, repo: str, branch: str = "main") -> bool: ...

    def get_diff(self, worktree_dir: Path, base: str = "origin/main") -> str: ...

    def list_changed_files(self, worktree_dir: Path, base: str = "origin/main") -> list[str]: ...

    def list_changed_entries(self, worktree_dir: Path, base: str = "origin/main") -> list[DiffPathEntry]: ...

    def get_test_file_snapshots(
        self,
        worktree_dir: Path,
        entries: list[DiffPathEntry],
        base: str = "origin/main",
    ) -> dict[str, tuple[str | None, str | None]]: ...

    def watch_workflow_run(
        self,
        repo: str,
        branch: str,
        workflow: str,
        timeout_seconds: int = 3600,
        commit_sha: str = "",
    ) -> tuple[bool | None, str, int | None]: ...

    def download_run_artifact(
        self,
        repo: str,
        run_id: int,
        dest_dir: Path,
    ) -> list[Path]: ...

    def create_github_release(
        self,
        repo: str,
        tag: str,
        title: str,
        notes: str,
        files: list[Path],
        prerelease: bool = True,
    ) -> tuple[bool, str]: ...

    def next_preview_tag(
        self,
        repo: str,
        tag_prefix: str = "v1.0.0-preview.",
    ) -> str: ...


class SubprocessGitGh:
    """Production implementation calling real `git` and `gh` CLIs."""

    def fetch_origin(self, local_repo: Path) -> tuple[int, str, str]:
        rc, out, err, _ = run_subprocess_with_timeout(["git", "fetch", "origin"], cwd=local_repo, timeout_seconds=120)
        return rc, out, err

    def remote_branch_exists(self, local_repo: Path, branch: str) -> bool:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=local_repo,
            timeout_seconds=60,
        )
        return rc == 0 and bool(out.strip())

    def remote_branch_sha(self, local_repo: Path, branch: str) -> str | None:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=local_repo,
            timeout_seconds=60,
        )
        if rc == 0 and out.strip():
            return out.split()[0]
        return None

    def local_head_sha(self, worktree_dir: Path) -> str:
        rc, out, _, _ = run_subprocess_with_timeout(["git", "rev-parse", "HEAD"], cwd=worktree_dir, timeout_seconds=30)
        return out.strip() if rc == 0 else ""

    def prepare_worktree(
        self,
        local_repo: Path,
        worktree_dir: Path,
        branch: str,
        base_branch: str = "main",
    ) -> Path:
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        self.fetch_origin(local_repo)

        if worktree_dir.exists():
            rc, out, err, _ = run_subprocess_with_timeout(
                ["git", "branch", "--show-current"], cwd=worktree_dir, timeout_seconds=30,
            )
            rc_root, root, _, _ = run_subprocess_with_timeout(
                ["git", "rev-parse", "--show-toplevel"], cwd=worktree_dir, timeout_seconds=30,
            )
            rc_common, common, _, _ = run_subprocess_with_timeout(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=worktree_dir, timeout_seconds=30,
            )
            rc_repo, repo_common, _, _ = run_subprocess_with_timeout(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=local_repo, timeout_seconds=30,
            )
            if (rc != 0 or out.strip() != branch or rc_root != 0 or Path(root.strip()).resolve() != worktree_dir.resolve()
                    or rc_common != 0 or rc_repo != 0 or Path(common.strip()).resolve() != Path(repo_common.strip()).resolve()):
                raise RuntimeError(f"Existing worktree does not match branch {branch}: {err}")
            remote = self.remote_branch_sha(local_repo, branch)
            if remote and self.local_head_sha(worktree_dir) != remote:
                def git(*args: str) -> str:
                    rc_git, output, error, _ = run_subprocess_with_timeout(
                        ["git", *args], cwd=worktree_dir, timeout_seconds=120,
                    )
                    if rc_git != 0:
                        raise RuntimeError(f"Could not reconcile worktree: {error or output}")
                    return output.strip()

                # Preserve tracked and untracked work before integrating the remote head.
                if git("status", "--porcelain"):
                    git("add", "-A")
                    git("commit", "-m", "nightshift: checkpoint before resume")
                git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
                git("merge", "--no-edit", f"origin/{branch}")
                if self.local_head_sha(worktree_dir) != self.remote_branch_sha(local_repo, branch):
                    if branch.strip().lower() in ("main", "master"):
                        raise RuntimeError("Cannot push a reconciliation to a protected branch")
                    git("push", "origin", branch)
                if self.local_head_sha(worktree_dir) != self.remote_branch_sha(local_repo, branch):
                    raise RuntimeError("Remote branch changed during worktree reconciliation")
            return worktree_dir
        run_subprocess_with_timeout(["git", "worktree", "prune"], cwd=local_repo, timeout_seconds=60)

        base = f"origin/{base_branch}"
        if self.remote_branch_exists(local_repo, branch):
            run_subprocess_with_timeout(
                ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                cwd=local_repo,
                timeout_seconds=120,
            )
            base = f"origin/{branch}"

        rc, _, err, _ = run_subprocess_with_timeout(
            ["git", "worktree", "add", "-B", branch, str(worktree_dir), base],
            cwd=local_repo,
            timeout_seconds=120,
        )
        if rc != 0:
            raise RuntimeError(f"git worktree add failed: {err}")

        # Configure info/exclude to ignore .nightshift_prompt.md and .nightshift/handoff.md
        rc_gd, out_gd, _, _ = run_subprocess_with_timeout(["git", "rev-parse", "--git-common-dir"], cwd=worktree_dir, timeout_seconds=30)
        if rc_gd == 0 and out_gd:
            exclude_path = Path(out_gd) / "info" / "exclude"
            if not exclude_path.is_absolute():
                exclude_path = worktree_dir / exclude_path
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
            lines_to_add = [".nightshift_prompt.md", ".nightshift/handoff.md", ".nightshift/*"]
            new_lines = [l for l in lines_to_add if l not in existing]
            if new_lines:
                with open(exclude_path, "a", encoding="utf-8") as f:
                    f.write("\n" + "\n".join(new_lines) + "\n")

        return worktree_dir

    def remove_worktree(self, local_repo: Path, worktree_dir: Path) -> None:
        run_subprocess_with_timeout(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=local_repo, timeout_seconds=60)
        shutil.rmtree(worktree_dir, ignore_errors=True)
        run_subprocess_with_timeout(["git", "worktree", "prune"], cwd=local_repo, timeout_seconds=60)

    def find_open_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,url,headRefOid,mergeable,mergeStateStatus"],
            timeout_seconds=60,
        )
        if rc == 0 and out:
            try:
                prs = json.loads(out)
                if prs:
                    return prs[0]
            except Exception:
                pass
        return None

    def find_merged_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "merged", "--json", "number,url,mergeCommit", "--limit", "1"],
            timeout_seconds=60,
        )
        if rc == 0 and out:
            try:
                prs = json.loads(out)
                if prs:
                    return prs[0]
            except Exception:
                pass
        return None

    def watch_pr_checks(
        self,
        repo: str,
        pr_number: int,
        worktree_dir: Path,
        timeout_seconds: int = 3600,
    ) -> tuple[bool, str]:
        rc, out, err, _ = run_subprocess_with_timeout(
            ["gh", "pr", "checks", str(pr_number), "--repo", repo, "--watch"],
            cwd=worktree_dir,
            timeout_seconds=timeout_seconds,
        )
        return (rc == 0, out or err)

    def get_failed_ci_log(self, repo: str, branch: str) -> str:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "run", "list", "--repo", repo, "--branch", branch, "--limit", "5", "--json", "databaseId,name,conclusion,status"],
            timeout_seconds=60,
        )
        if rc != 0 or not out:
            return "Could not list GitHub Actions runs."
        try:
            runs = json.loads(out)
            failed = [r for r in runs if r.get("conclusion") == "failure"]
            if not failed:
                return "No failed workflow logs found."
            run_id = str(failed[0]["databaseId"])
            rc2, log_out, err2, _ = run_subprocess_with_timeout(
                ["gh", "run", "view", run_id, "--repo", repo, "--log-failed"],
                timeout_seconds=120,
            )
            lines = (log_out or err2).splitlines()[-120:]
            return f"Failed run {run_id} ({failed[0].get('name')}):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error retrieving failed CI log: {e}"

    def merge_pr(
        self,
        repo: str,
        pr_number: int,
        local_repo: Path,
        strategy: str = "--merge",
    ) -> tuple[bool, str]:
        rc, out, err, _ = run_subprocess_with_timeout(
            ["gh", "pr", "merge", str(pr_number), "--repo", repo, strategy, "--delete-branch"],
            cwd=local_repo,
            timeout_seconds=120,
        )
        return (rc == 0, out or err)

    def create_checkpoint_commit_and_push(
        self,
        worktree_dir: Path,
        branch: str,
        message: str,
    ) -> str | None:
        if branch.strip().lower() in ("main", "master"):
            raise ValueError(f"Hard guard violation: direct push to protected branch '{branch}' is forbidden.")
        run_subprocess_with_timeout(["git", "add", "-A"], cwd=worktree_dir, timeout_seconds=60)
        rc_c, _, _, _ = run_subprocess_with_timeout(["git", "commit", "-m", message], cwd=worktree_dir, timeout_seconds=60)
        if rc_c == 0:
            run_subprocess_with_timeout(["git", "push", "origin", branch], cwd=worktree_dir, timeout_seconds=120)
            return self.local_head_sha(worktree_dir)
        return None

    def merge_main_into_branch(self, worktree_dir: Path) -> tuple[bool, str]:
        run_subprocess_with_timeout(["git", "fetch", "origin", "main"], cwd=worktree_dir, timeout_seconds=60)
        rc, out, err, _ = run_subprocess_with_timeout(
            ["git", "merge", "origin/main", "--no-edit"],
            cwd=worktree_dir,
            timeout_seconds=60,
        )
        return (rc == 0, out or err)

    def revert_via_pr(self, repo: str, local_repo: Path, commit_sha: str) -> tuple[bool, str]:
        """Create a revert on an isolated branch and merge only after its checks pass."""
        branch = f"nightshift/revert-{commit_sha}"
        worktree = local_repo.parent / f"nightshift-revert-{commit_sha}"
        if self.find_merged_pr(repo, branch):
            return True, "Revert PR already merged"
        try:
            self.prepare_worktree(local_repo, worktree, branch)
            pr = self.find_open_pr(repo, branch)
            if not pr:
                rc, out, err, _ = run_subprocess_with_timeout(
                    ["git", "revert", "-m", "1", commit_sha, "--no-edit"], cwd=worktree, timeout_seconds=60,
                )
                if rc != 0:
                    return False, out or err
                rc, out, err, _ = run_subprocess_with_timeout(
                    ["git", "push", "-u", "origin", branch], cwd=worktree, timeout_seconds=120,
                )
                if rc != 0:
                    return False, out or err
                rc, out, err, _ = run_subprocess_with_timeout(
                    ["gh", "pr", "create", "--repo", repo, "--base", "main", "--head", branch,
                     "--title", f"Revert {commit_sha[:7]}", "--body", "Revert a merge whose main build failed."],
                    cwd=worktree, timeout_seconds=60,
                )
                if rc != 0:
                    return False, out or err
                pr = self.find_open_pr(repo, branch)
            if not pr or not pr.get("headRefOid"):
                return False, "Could not verify revert PR head"
            passed, msg = self.watch_pr_checks(repo, pr["number"], worktree)
            latest = self.find_open_pr(repo, branch)
            if not passed or not latest or latest.get("headRefOid") != pr["headRefOid"]:
                return False, f"Revert PR checks failed or head changed: {msg}"
            return self.merge_pr(repo, pr["number"], local_repo)
        finally:
            if worktree.exists():
                self.remove_worktree(local_repo, worktree)

    def check_branch_protection(self, repo: str, branch: str = "main") -> bool:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "api", f"repos/{repo}/branches/{branch}/protection"],
            timeout_seconds=60,
        )
        return rc == 0 and bool(out.strip())

    def get_diff(self, worktree_dir: Path, base: str = "origin/main") -> str:
        rc, out, _, _ = run_subprocess_with_timeout(["git", "diff", base], cwd=worktree_dir, timeout_seconds=60)
        return out if rc == 0 else ""

    def list_changed_files(self, worktree_dir: Path, base: str = "origin/main") -> list[str]:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "diff", "--name-only", base],
            cwd=worktree_dir,
            timeout_seconds=60,
        )
        return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []

    def list_changed_entries(self, worktree_dir: Path, base: str = "origin/main") -> list[DiffPathEntry]:
        """Extract rename/copy/delete-aware diff entries using ``git diff --name-status -M -C``."""
        from nightshift.engine.pipeline_contracts import parse_name_status_diff

        rc, out, _, _ = run_subprocess_with_timeout(
            ["git", "diff", "--name-status", "-M", "-C", base],
            cwd=worktree_dir,
            timeout_seconds=60,
        )
        return parse_name_status_diff(out) if rc == 0 else []

    def get_test_file_snapshots(
        self,
        worktree_dir: Path,
        entries: list[DiffPathEntry],
        base: str = "origin/main",
    ) -> dict[str, tuple[str | None, str | None]]:
        """Capture ``(before_source, after_source)`` for modified/deleted/renamed test files."""
        from nightshift.engine.pipeline_contracts import is_test_file_path

        snapshots: dict[str, tuple[str | None, str | None]] = {}
        for entry in entries:
            candidate_paths = entry.all_paths()
            if not any(is_test_file_path(p) for p in candidate_paths):
                continue
            before_ref_path = entry.old_path or entry.path
            before_src: str | None = None
            if entry.status != "A":
                rc, out, _, _ = run_subprocess_with_timeout(
                    ["git", "show", f"{base}:{before_ref_path}"],
                    cwd=worktree_dir,
                    timeout_seconds=30,
                )
                if rc == 0:
                    before_src = out
            after_src: str | None = None
            if entry.status != "D":
                target_file = worktree_dir / entry.path
                if target_file.exists() and target_file.is_file():
                    after_src = target_file.read_text(encoding="utf-8", errors="replace")
            snapshots[before_ref_path if entry.status == "D" else entry.path] = (before_src, after_src)
        return snapshots

    def watch_workflow_run(
        self,
        repo: str,
        branch: str,
        workflow: str,
        timeout_seconds: int = 3600,
        commit_sha: str = "",
    ) -> tuple[bool | None, str, int | None]:
        if not commit_sha:
            return None, "Merged commit SHA is required", None
        rc, out, err, _ = run_subprocess_with_timeout(
            ["gh", "run", "list", "--repo", repo, "--branch", branch, "--workflow", workflow, "--commit", commit_sha, "--limit", "1", "--json", "databaseId,status,conclusion"],
            timeout_seconds=60,
        )
        if rc != 0 or not out:
            return None, f"Could not locate workflow run: {err}", None
        try:
            runs = json.loads(out)
            if not runs:
                return None, "No workflow runs found", None
            run_id = int(runs[0]["databaseId"])
            rc_w, out_w, err_w, _ = run_subprocess_with_timeout(
                ["gh", "run", "watch", str(run_id), "--repo", repo, "--exit-status"],
                timeout_seconds=timeout_seconds,
            )
            if rc_w == 0:
                return True, out_w or err_w, run_id
            rc_v, out_v, _, _ = run_subprocess_with_timeout(
                ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "status,conclusion"], timeout_seconds=60,
            )
            if rc_v == 0:
                result = json.loads(out_v)
                if result.get("status") == "completed" and result.get("conclusion") == "failure":
                    return False, out_w or err_w, run_id
            return None, out_w or err_w, run_id
        except Exception as e:
            return None, str(e), None

    def download_run_artifact(
        self,
        repo: str,
        run_id: int,
        dest_dir: Path,
    ) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        rc, _, err, _ = run_subprocess_with_timeout(
            ["gh", "run", "download", str(run_id), "--repo", repo, "--dir", str(dest_dir)],
            timeout_seconds=300,
        )
        if rc != 0:
            raise RuntimeError(f"Artifact download failed: {err}")
        return [p for p in dest_dir.rglob("*") if p.is_file()]

    def create_github_release(
        self,
        repo: str,
        tag: str,
        title: str,
        notes: str,
        files: list[Path],
        prerelease: bool = True,
    ) -> tuple[bool, str]:
        cmd = ["gh", "release", "create", tag]
        for f in files:
            cmd.append(str(f))
        cmd.extend(["--repo", repo, "--title", title, "--notes", notes])
        if prerelease:
            cmd.append("--prerelease")
        rc, out, err, _ = run_subprocess_with_timeout(cmd, timeout_seconds=180)
        return (rc == 0, out or err)

    def next_preview_tag(
        self,
        repo: str,
        tag_prefix: str = "v1.0.0-preview.",
    ) -> str:
        rc, out, _, _ = run_subprocess_with_timeout(
            ["gh", "release", "list", "--repo", repo, "--limit", "40", "--json", "tagName"],
            timeout_seconds=60,
        )
        max_num = 0
        escaped_prefix = re.escape(tag_prefix)
        pattern = re.compile(rf"^{escaped_prefix}(\d+)$")
        if rc == 0 and out:
            try:
                for item in json.loads(out):
                    m = pattern.match(item.get("tagName", ""))
                    if m:
                        max_num = max(max_num, int(m.group(1)))
            except Exception:
                pass
        return f"{tag_prefix}{max_num + 1}"


class FakeGitGh:
    """In-memory fake VCS implementation for completely offline, deterministic testing."""

    def __init__(self) -> None:
        self.branches: dict[str, str] = {"main": "sha_main_001"}
        self.prs: dict[str, dict[str, Any]] = {}
        self.merged_prs: dict[str, dict[str, Any]] = {}
        self.ci_results: dict[int, list[bool]] = {}  # list of outcomes per attempt
        self.ci_flaky: dict[int, bool] = {}
        self.failed_ci_logs: dict[str, str] = {}
        self.worktrees: dict[str, Path] = {}
        self.checkpoints: list[dict[str, Any]] = []
        self.reverts: list[dict[str, Any]] = []
        self.releases: list[dict[str, Any]] = []
        self.branch_protection: dict[str, bool] = {"main": True}
        self.diffs: dict[str, str] = {}
        self.changed_files: dict[str, list[str]] = {}
        self.diff_entries: dict[str, list[Any]] = {}
        self.test_file_snapshots: dict[str, dict[str, tuple[str | None, str | None]]] = {}
        self.workflow_runs: dict[str, bool] = {"Android build": True, "Desktop build": True}
        self.artifacts_to_create: list[str] = ["NightShift-test.zip"]
        self.merge_conflict: bool = False
        self._next_pr_number = 100

    def add_pr(self, branch: str, number: int | None = None, url: str | None = None, head_sha: str = "sha_head_1") -> dict[str, Any]:
        num = number or self._next_pr_number
        self._next_pr_number += 1
        pr = {
            "number": num,
            "url": url or f"https://github.com/test/repo/pull/{num}",
            "headRefOid": head_sha,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        self.prs[branch] = pr
        self.branches[branch] = head_sha
        return pr

    def fetch_origin(self, local_repo: Path) -> tuple[int, str, str]:
        return 0, "From github.com:test/repo\n * [new branch] main -> origin/main", ""

    def remote_branch_exists(self, local_repo: Path, branch: str) -> bool:
        return branch in self.branches

    def remote_branch_sha(self, local_repo: Path, branch: str) -> str | None:
        return self.branches.get(branch)

    def local_head_sha(self, worktree_dir: Path) -> str:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return self.branches.get(branch, "sha_local_default")
        if len(self.prs) == 1:
            return str(next(iter(self.prs.values())).get("headRefOid") or "")
        return "sha_local_default"

    def prepare_worktree(
        self,
        local_repo: Path,
        worktree_dir: Path,
        branch: str,
        base_branch: str = "main",
    ) -> Path:
        worktree_dir.mkdir(parents=True, exist_ok=True)
        if branch not in self.branches:
            self.branches[branch] = f"sha_{branch}_initial"
        self.worktrees[branch] = worktree_dir
        return worktree_dir

    def remove_worktree(self, local_repo: Path, worktree_dir: Path) -> None:
        to_del = [b for b, wt in self.worktrees.items() if wt == worktree_dir]
        for b in to_del:
            del self.worktrees[b]
        shutil.rmtree(worktree_dir, ignore_errors=True)

    def find_open_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        return self.prs.get(branch)

    def find_merged_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        return self.merged_prs.get(branch)

    def watch_pr_checks(
        self,
        repo: str,
        pr_number: int,
        worktree_dir: Path,
        timeout_seconds: int = 3600,
    ) -> tuple[bool, str]:
        if pr_number in self.ci_flaky and self.ci_flaky[pr_number]:
            # Flaky check: passes on re-run
            self.ci_flaky[pr_number] = False
            return False, "Failed first attempt (flaky)"
        if pr_number in self.ci_results:
            results = self.ci_results[pr_number]
            if results:
                passed = results.pop(0)
                return passed, "CI passed" if passed else "CI failed"
        return True, "All checks passed"

    def get_failed_ci_log(self, repo: str, branch: str) -> str:
        return self.failed_ci_logs.get(branch, "Build failed at Task :app:compileKotlin: Type mismatch error")

    def merge_pr(
        self,
        repo: str,
        pr_number: int,
        local_repo: Path,
        strategy: str = "--merge",
    ) -> tuple[bool, str]:
        for branch, pr in list(self.prs.items()):
            if pr["number"] == pr_number:
                pr["mergeCommit"] = {"oid": "sha_merge_123"}
                self.merged_prs[branch] = pr
                del self.prs[branch]
                return True, f"Merged PR #{pr_number} cleanly into main."
        return False, f"PR #{pr_number} not found"

    def create_checkpoint_commit_and_push(
        self,
        worktree_dir: Path,
        branch: str,
        message: str,
    ) -> str | None:
        if branch.strip().lower() in ("main", "master"):
            raise ValueError(f"Hard guard violation: direct push to protected branch '{branch}' is forbidden.")
        new_sha = f"sha_{branch}_{len(self.checkpoints) + 1}"
        self.branches[branch] = new_sha
        self.checkpoints.append({"branch": branch, "message": message, "sha": new_sha, "worktree": str(worktree_dir)})
        if branch in self.prs:
            self.prs[branch]["headRefOid"] = new_sha
        return new_sha

    def merge_main_into_branch(self, worktree_dir: Path) -> tuple[bool, str]:
        if self.merge_conflict:
            return False, "CONFLICT (content): Merge conflict in src/App.kt"
        return True, "Already up to date."

    def revert_via_pr(self, repo: str, local_repo: Path, commit_sha: str) -> tuple[bool, str]:
        branch = f"nightshift/revert-{commit_sha}"
        self.reverts.append({"commit_sha": commit_sha, "branch": branch})
        pr = self.find_open_pr(repo, branch) or self.add_pr(branch)
        passed, msg = self.watch_pr_checks(repo, pr["number"], local_repo)
        if not passed:
            return False, msg
        return self.merge_pr(repo, pr["number"], local_repo)

    def check_branch_protection(self, repo: str, branch: str = "main") -> bool:
        return self.branch_protection.get(branch, True)

    def get_diff(self, worktree_dir: Path, base: str = "origin/main") -> str:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return self.diffs.get(branch, "diff --git a/file b/file\n+new content")
        return self.diffs.get("default", "")

    def list_changed_files(self, worktree_dir: Path, base: str = "origin/main") -> list[str]:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return self.changed_files.get(branch, ["src/main.ts"])
        return self.changed_files.get("default", [])

    def list_changed_entries(self, worktree_dir: Path, base: str = "origin/main") -> list[DiffPathEntry]:
        from nightshift.engine.pipeline_contracts import DiffPathEntry

        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                if branch in self.diff_entries:
                    return list(self.diff_entries[branch])
                files = self.changed_files.get(branch, ["src/main.ts"])
                return [DiffPathEntry(status="M", path=f) for f in files]
        if "default" in self.diff_entries:
            return list(self.diff_entries["default"])
        return [DiffPathEntry(status="M", path=f) for f in self.changed_files.get("default", [])]

    def get_test_file_snapshots(
        self,
        worktree_dir: Path,
        entries: list[DiffPathEntry],
        base: str = "origin/main",
    ) -> dict[str, tuple[str | None, str | None]]:
        for branch, wt in self.worktrees.items():
            if str(wt) == str(worktree_dir):
                return dict(self.test_file_snapshots.get(branch, {}))
        return dict(self.test_file_snapshots.get("default", {}))

    def watch_workflow_run(
        self,
        repo: str,
        branch: str,
        workflow: str,
        timeout_seconds: int = 3600,
        commit_sha: str = "",
    ) -> tuple[bool, str, int | None]:
        success = self.workflow_runs.get(workflow, True)
        return success, "Run completed" if success else "Workflow run failed", 9999

    def download_run_artifact(
        self,
        repo: str,
        run_id: int,
        dest_dir: Path,
    ) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        for name in self.artifacts_to_create:
            p = dest_dir / name
            p.write_bytes(b"PK\x03\x04mock_zip_content")
            created.append(p)
        return created

    def create_github_release(
        self,
        repo: str,
        tag: str,
        title: str,
        notes: str,
        files: list[Path],
        prerelease: bool = True,
    ) -> tuple[bool, str]:
        rel = {
            "tag": tag,
            "title": title,
            "notes": notes,
            "files": [str(f) for f in files],
            "prerelease": prerelease,
        }
        self.releases.append(rel)
        return True, f"https://github.com/{repo}/releases/tag/{tag}"

    def next_preview_tag(
        self,
        repo: str,
        tag_prefix: str = "v1.0.0-preview.",
    ) -> str:
        num = len(self.releases) + 1
        return f"{tag_prefix}{num}"
