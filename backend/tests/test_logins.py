"""Table-driven tests for Login health check system (BLUEPRINT 5f)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nightshift.engine.logins import LoginCheckResult, LoginHealthChecker
from nightshift.quota.ledger import QuotaLedger
from nightshift.store import Store

TABLE_TEST_CASES = [
    # tool, runner_output, expected_state
    ("git", (0, "abc123\tHEAD", "", False), "Ready"),
    ("git", (128, "", "fatal: Authentication failed for 'https://github.com/...'", False), "Logged out"),
    ("git", (128, "", "fatal: unable to access '...': Could not resolve host: github.com", False), "Unknown (network)"),
    ("gh", (0, "Logged in to github.com account octocat", "", False), "Ready"),
    ("gh", (1, "", "You are not logged into any GitHub hosts. Run gh auth login", False), "Logged out"),
    ("claude", (0, '{"loggedIn": true}', "", False), "Ready"),
    ("claude", (0, '{"loggedIn": false}', "", False), "Logged out"),
    ("codex", (0, "Logged in as test@example.com", "", False), "Ready"),
    ("codex", (1, "", "Not logged in. Run codex login", False), "Logged out"),
    ("gemini", (0, '{"response": "OK"}', "", False), "Ready"),
    ("gemini", (1, "", "429 Too Many Requests: quota exceeded. Reset at 04:30.", False), "Limit reached (back at 04:30)"),
    ("gemini", (1, "", "Error: Not authenticated. Run gemini auth", False), "Logged out"),
    ("gemini", (1, "", "getaddrinfo ENOTFOUND: DNS lookup failed", False), "Unknown (network)"),
    ("agy", (0, '{"response": "OK"}', "", False), "Ready"),
    ("agy", (1, "", "Usage limit reached. Back at 05:00.", False), "Limit reached (back at 05:00)"),
    ("agy", (1, "", "Error: unauthorized or expired token", False), "Logged out"),
]


@pytest.mark.parametrize("tool,output,expected_state", TABLE_TEST_CASES)
def test_login_health_classification(
    tmp_path: Path,
    tool: str,
    output: tuple[int, str, str, bool],
    expected_state: str,
) -> None:
    db = tmp_path / f"login_{tool}.db"
    with Store(db) as store:
        store.migrate()
        quota = QuotaLedger(store)

        def mock_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
            # For gh Ready, second call to `gh api user` succeeds
            if tool == "gh" and "api" in cmd:
                return (0, '{"login": "octocat"}', "", False)
            return output

        # Mock resolve_bin to simulate binary installed
        with patch("nightshift.engine.logins.resolve_bin", return_value=f"C:\\bin\\{tool}.exe"):
            checker = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=mock_runner)
            result = checker.check_tool(tool, force=True)
            assert result.tool == tool
            assert result.state == expected_state

            # Verify persisted in store
            persisted = store.get_latest_login_checks()
            assert tool in persisted
            assert persisted[tool]["state"] == expected_state


def test_login_health_not_installed(tmp_path: Path) -> None:
    db = tmp_path / "not_installed.db"
    with Store(db) as store:
        store.migrate()
        with patch("nightshift.engine.logins.resolve_bin", return_value=None):
            checker = LoginHealthChecker(store=store)
            for tool in ["git", "gh", "claude", "codex", "gemini", "agy"]:
                res = checker.check_tool(tool, force=True)
                assert res.state == "Not installed"


def test_login_health_probe_skipped_during_cooldown(tmp_path: Path) -> None:
    db = tmp_path / "cooldown_skip.db"
    with Store(db) as store:
        store.migrate()
        quota = QuotaLedger(store, default_cooldown_seconds=1800)
        # Put gemini in active cooldown
        quota.record_quota_error("gemini", "default", "Quota exceeded. Try again in 30 minutes.")

        probe_called = False

        def mock_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
            nonlocal probe_called
            probe_called = True
            return (0, "OK", "", False)

        checker = LoginHealthChecker(store=store, quota_ledger=quota, command_runner=mock_runner)
        res = checker.check_tool("gemini", force=True)

        assert not probe_called  # Probe must NOT be executed during active cooldown!
        assert res.state.startswith("Limit reached")
        assert res.back_at is not None


def test_login_health_caching(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    with Store(db) as store:
        store.migrate()
        call_count = 0

        def mock_runner(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
            nonlocal call_count
            call_count += 1
            return (0, '{"loggedIn": true}', "", False)

        with patch("nightshift.engine.logins.resolve_bin", return_value="C:\\bin\\claude.exe"):
            checker = LoginHealthChecker(store=store, command_runner=mock_runner)
            res1 = checker.check_tool("claude")
            assert res1.state == "Ready"
            assert call_count == 1

            # Second call within 60 minutes hits cache
            res2 = checker.check_tool("claude")
            assert res2.state == "Ready"
            assert call_count == 1

            # Force bypasses cache
            res3 = checker.check_tool("claude", force=True)
            assert res3.state == "Ready"
            assert call_count == 2
