from pathlib import Path

import pytest

from nightshift.engine import vcs


def test_checkpoint_pushes_existing_head_when_worktree_is_clean(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1:] == ["branch", "--show-current"]:
            return 0, "feat/example", "", False
        return 0, "", "", False

    monkeypatch.setattr(vcs, "run_subprocess_with_timeout", run)
    client = vcs.SubprocessGitGh()
    monkeypatch.setattr(client, "local_head_sha", lambda _: "head")
    assert client.create_checkpoint_commit_and_push(tmp_path, "feat/example", "checkpoint") == "head"
    assert ["git", "push", "origin", "feat/example"] in calls
    assert not any(call[1] == "commit" for call in calls)


def test_checkpoint_rejects_failed_push(monkeypatch, tmp_path: Path) -> None:
    def run(args, **kwargs):
        if args[1:] == ["branch", "--show-current"]:
            return 0, "feat/example", "", False
        if args[1] == "push":
            return 1, "", "offline", False
        return 0, "", "", False

    monkeypatch.setattr(vcs, "run_subprocess_with_timeout", run)
    with pytest.raises(RuntimeError, match="Cannot push checkpoint: offline"):
        vcs.SubprocessGitGh().create_checkpoint_commit_and_push(tmp_path, "feat/example", "checkpoint")


def test_checkpoint_refuses_wrong_branch(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return 0, "main", "", False

    monkeypatch.setattr(vcs, "run_subprocess_with_timeout", run)
    with pytest.raises(RuntimeError, match="worktree is not on feat/example"):
        vcs.SubprocessGitGh().create_checkpoint_commit_and_push(tmp_path, "feat/example", "checkpoint")
    assert len(calls) == 1


def test_checkpoint_commits_staged_work_before_push(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1:] == ["branch", "--show-current"]:
            return 0, "feat/example", "", False
        if args[1:] == ["diff", "--cached", "--quiet"]:
            return 1, "", "", False
        return 0, "", "", False

    monkeypatch.setattr(vcs, "run_subprocess_with_timeout", run)
    client = vcs.SubprocessGitGh()
    monkeypatch.setattr(client, "local_head_sha", lambda _: "new-head")
    assert client.create_checkpoint_commit_and_push(tmp_path, "feat/example", "checkpoint") == "new-head"
    assert calls.index(["git", "commit", "-m", "checkpoint"]) < calls.index(["git", "push", "origin", "feat/example"])
