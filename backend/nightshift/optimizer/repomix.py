"""repomix integration: pinned --compress repo map with token budget & git-exclude (BLUEPRINT 5g)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from nightshift.optimizer.tool_manager import ToolManager
from nightshift.runners.base import run_subprocess_with_timeout

CommandExecutorFn = Callable[[list[str], Path | None, dict[str, str], int], tuple[int, str, str, bool]]

REPO_MAP_RELATIVE_PATH = Path(".nightshift") / "repo_map.md"
EXCLUDED_PATTERNS = (
    ".nightshift/",
    ".nightshift_prompt.md",
)


def _default_exec(
    cmd: list[str],
    cwd: Path | None,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, str, str, bool]:
    return run_subprocess_with_timeout(cmd, cwd=cwd, timeout_seconds=timeout_seconds, env=env)


def resolve_git_info_exclude(worktree_dir: Path) -> Path:
    """Resolve the .git/info/exclude file for both standard repos and git worktrees."""
    dot_git = worktree_dir / ".git"
    if dot_git.is_file():
        content = dot_git.read_text(encoding="utf-8").strip()
        if content.lower().startswith("gitdir:"):
            git_dir_raw = content.split(":", 1)[1].strip()
            git_dir = Path(git_dir_raw)
            if not git_dir.is_absolute():
                git_dir = (worktree_dir / git_dir).resolve()
            return git_dir / "info" / "exclude"
    return dot_git / "info" / "exclude"


def ensure_worktree_git_excluded(worktree_dir: Path, patterns: tuple[str, ...] = EXCLUDED_PATTERNS) -> Path:
    """Add NightShift generated files to .git/info/exclude so they never pollute PR diffs."""
    exclude_path = resolve_git_info_exclude(worktree_dir)
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    lines = [line.strip() for line in existing.splitlines()]
    to_add = [p for p in patterns if p not in lines]
    if to_add:
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        exclude_path.write_text(existing + prefix + "\n".join(to_add) + "\n", encoding="utf-8")
    return exclude_path


def estimate_tokens(text: str) -> int:
    """Fast deterministic token count estimate (~4 chars per token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def truncate_to_token_budget(text: str, token_budget: int) -> str:
    """Cap repo map text to the configured token budget."""
    if token_budget <= 0:
        return ""
    max_chars = token_budget * 4
    if len(text) <= max_chars:
        return text
    suffix = "\n\n[Repo map truncated to stay within token budget.]\n"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


@dataclass(frozen=True)
class RepoMapResult:
    """Outcome of generating a compact repomix repo map for a phase."""

    ok: bool
    map_path: Path | None = None
    content: str = ""
    estimated_tokens: int = 0
    warning: str = ""


class RepomixAdapter:
    """Generates a git-excluded, token-budgeted compact repo map per phase."""

    def __init__(
        self,
        tool_manager: ToolManager,
        *,
        executor: CommandExecutorFn | None = None,
        default_token_budget: int = 12000,
    ) -> None:
        self._tool_manager = tool_manager
        self._executor = executor or _default_exec
        self._default_token_budget = default_token_budget

    def generate_repo_map(
        self,
        worktree_dir: Path,
        *,
        token_budget: int | None = None,
        timeout_seconds: int = 60,
    ) -> RepoMapResult:
        """Run pinned repomix --compress into .nightshift/repo_map.md (git-excluded)."""
        ver = self._tool_manager.verify_installed("repomix")
        if not ver.ok or ver.executable_path is None:
            return RepoMapResult(
                ok=False,
                warning=ver.warning or "repomix is not verified; skipping compact repo map.",
            )

        spec = self._tool_manager.get_spec("repomix")
        budget = token_budget if token_budget is not None else (spec.token_budget if spec else self._default_token_budget)

        ensure_worktree_git_excluded(worktree_dir)
        out_path = worktree_dir / REPO_MAP_RELATIVE_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(ver.executable_path),
            "--compress",
            "--output",
            str(out_path),
            "--style",
            "markdown",
            "--no-file-summary",
        ]
        env = self._tool_manager.build_process_env()
        rc, stdout, stderr, timed_out = self._executor(cmd, worktree_dir, env, timeout_seconds)
        if rc != 0 or timed_out:
            reason = "timed out" if timed_out else (stderr[:160] or f"exit code {rc}")
            return RepoMapResult(ok=False, warning=f"repomix failed ({reason}); continuing without repo map.")

        raw_content = out_path.read_text(encoding="utf-8") if out_path.exists() else stdout
        bounded = truncate_to_token_budget(raw_content.strip(), budget)
        out_path.write_text(bounded, encoding="utf-8")

        return RepoMapResult(
            ok=True,
            map_path=out_path,
            content=bounded,
            estimated_tokens=estimate_tokens(bounded),
        )

    @staticmethod
    def inject_into_prompt(prompt: str, repo_map: str) -> str:
        """Inject the compact repo map into the phase prompt cleanly."""
        if not repo_map.strip():
            return prompt
        header = (
            "COMPACT REPO MAP (generated by pinned repomix --compress, git-excluded):\n"
            "Use this map to locate files directly instead of running broad exploratory listings.\n\n"
            f"{repo_map.strip()}\n\n"
            "--- END REPO MAP ---\n\n"
        )
        return header + prompt
