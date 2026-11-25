"""Login health check system for all CLI tools and accounts (BLUEPRINT 5f).

Classifies each tool as:
  - Ready
  - Logged out
  - Limit reached (back at HH:MM)
  - Not installed
  - Unknown (network)

Never reads, stores, or logs credential contents or tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from nightshift.quota.ledger import QuotaLedger, parse_reset_time
from nightshift.runners.base import resolve_bin, run_subprocess_with_timeout
from nightshift.store import Store

CommandRunner = Callable[[list[str], int], tuple[int, str, str, bool]]


@dataclass(frozen=True)
class LoginCheckResult:
    tool: str
    state: str  # Ready | Logged out | Limit reached (back at HH:MM) | Not installed | Unknown (network)
    detail: str
    back_at: str | None = None
    checked_at: str = ""


class LoginHealthChecker:
    """Checks credentials and health across all supported coding CLIs and VCS tools."""

    CACHE_TTL_MINUTES = 60
    RUN_RECHECK_TTL_MINUTES = 30

    def __init__(
        self,
        store: Store | None = None,
        quota_ledger: QuotaLedger | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._store = store
        self._quota_ledger = quota_ledger
        self._runner = command_runner or (lambda cmd, to: run_subprocess_with_timeout(cmd, timeout_seconds=to))
        self._cache: dict[str, tuple[datetime, LoginCheckResult]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def check_tool(
        self,
        tool: str,
        repo: str = "umairgemini1192-netizen/drivelift-android",
        force: bool = False,
        in_run: bool = False,
    ) -> LoginCheckResult:
        """Check login health of a specific tool, respecting cache and cooldowns."""
        now = datetime.now(timezone.utc)
        ttl_minutes = self.RUN_RECHECK_TTL_MINUTES if in_run else self.CACHE_TTL_MINUTES
        if not force and tool in self._cache:
            cached_time, cached_res = self._cache[tool]
            if now - cached_time < timedelta(minutes=ttl_minutes):
                return cached_res

        # Check if cooling down first (skip probes during cooldown per BLUEPRINT 5f)
        if self._quota_ledger is not None and self._quota_ledger.is_cooling_down(tool):
            back_at = self._quota_ledger.get_reset_at(tool)
            back_str = back_at.strftime("%H:%M") if back_at else "soon"
            state = f"Limit reached (back at {back_str})"
            res = LoginCheckResult(
                tool=tool,
                state=state,
                detail=f"Cooldown active from prior quota limit until {back_str}",
                back_at=back_at.isoformat() if back_at else None,
                checked_at=now.isoformat(),
            )
            self._cache[tool] = (now, res)
            if self._store:
                self._store.record_login_check(tool, res.state, res.detail, res.back_at)
            return res

        checker_fn = getattr(self, f"_check_{tool}", None)
        if checker_fn is None:
            res = LoginCheckResult(
                tool=tool,
                state="Unknown (network)",
                detail=f"Unknown tool: {tool}",
                checked_at=now.isoformat(),
            )
        else:
            res = checker_fn(repo=repo)

        self._cache[tool] = (now, res)
        if self._store:
            self._store.record_login_check(tool, res.state, res.detail, res.back_at)
        return res

    def check_all(
        self,
        tools: list[str] | None = None,
        repo: str = "umairgemini1192-netizen/drivelift-android",
        force: bool = False,
    ) -> dict[str, LoginCheckResult]:
        target_tools = tools or ["git", "gh", "claude", "codex", "gemini", "agy"]
        results: dict[str, LoginCheckResult] = {}
        for t in target_tools:
            results[t] = self.check_tool(t, repo=repo, force=force)
        return results

    # Free check: git
    def _check_git(self, repo: str) -> LoginCheckResult:
        now = datetime.now(timezone.utc).isoformat()
        if not resolve_bin("git"):
            return LoginCheckResult(tool="git", state="Not installed", detail="git executable not found in PATH", checked_at=now)

        rc, out, err, timed_out = self._runner(["git", "ls-remote", f"https://github.com/{repo}.git", "HEAD"], 30)
        if timed_out or rc == 124:
            return LoginCheckResult(tool="git", state="Unknown (network)", detail="git ls-remote timed out", checked_at=now)
        if rc == 0:
            return LoginCheckResult(tool="git", state="Ready", detail="git ls-remote succeeded", checked_at=now)

        combined = f"{out}\n{err}".lower()
        if any(w in combined for w in ["could not resolve host", "network", "timed out", "connection"]):
            return LoginCheckResult(tool="git", state="Unknown (network)", detail="git ls-remote failed due to network issue", checked_at=now)
        if any(w in combined for w in ["authentication failed", "fatal: repository", "permission denied", "terminal prompts disabled"]):
            return LoginCheckResult(tool="git", state="Logged out", detail="git authentication failed for repository", checked_at=now)
        return LoginCheckResult(tool="git", state="Unknown (network)", detail=f"git ls-remote returned {rc}: {err[:100]}", checked_at=now)

    # Free check: gh
    def _check_gh(self, repo: str) -> LoginCheckResult:
        now = datetime.now(timezone.utc).isoformat()
        if not resolve_bin("gh"):
            return LoginCheckResult(tool="gh", state="Not installed", detail="gh executable not found in PATH", checked_at=now)

        rc, out, err, timed_out = self._runner(["gh", "auth", "status"], 30)
        combined = f"{out}\n{err}".lower()
        if timed_out:
            return LoginCheckResult(tool="gh", state="Unknown (network)", detail="gh auth status timed out", checked_at=now)
        if "logged in to github" in combined or "logged in to" in combined or rc == 0:
            # Check gh api user
            rc_api, out_api, err_api, _ = self._runner(["gh", "api", "user"], 30)
            if rc_api == 0:
                return LoginCheckResult(tool="gh", state="Ready", detail="gh authenticated and verified with GitHub API", checked_at=now)
            combined_api = f"{out_api}\n{err_api}".lower()
            if "rate limit" in combined_api or "429" in combined_api:
                reset = parse_reset_time(combined_api)
                back_str = reset[11:16] if reset and len(reset) >= 16 else "soon"
                return LoginCheckResult(tool="gh", state=f"Limit reached (back at {back_str})", detail="gh API rate limit reached", back_at=reset, checked_at=now)
            if "bad credentials" in combined_api or rc_api == 401:
                return LoginCheckResult(tool="gh", state="Logged out", detail="gh credentials expired or invalid", checked_at=now)
            return LoginCheckResult(tool="gh", state="Ready", detail="gh auth status is valid", checked_at=now)

        if "not logged in" in combined or "you are not logged into" in combined:
            return LoginCheckResult(tool="gh", state="Logged out", detail="gh is not logged in", checked_at=now)
        return LoginCheckResult(tool="gh", state="Unknown (network)", detail=f"gh check returned {rc}: {err[:100]}", checked_at=now)

    # Free check: claude
    def _check_claude(self, repo: str) -> LoginCheckResult:
        now = datetime.now(timezone.utc).isoformat()
        if not resolve_bin("claude"):
            return LoginCheckResult(tool="claude", state="Not installed", detail="claude executable not found in PATH", checked_at=now)

        rc, out, err, timed_out = self._runner(["claude", "auth", "status"], 30)
        if timed_out:
            return LoginCheckResult(tool="claude", state="Unknown (network)", detail="claude auth status timed out", checked_at=now)

        combined = f"{out}\n{err}"
        try:
            data = json.loads(out)
            if data.get("loggedIn") is True:
                return LoginCheckResult(tool="claude", state="Ready", detail="Claude Code logged in", checked_at=now)
            if data.get("loggedIn") is False:
                return LoginCheckResult(tool="claude", state="Logged out", detail="Claude Code not logged in", checked_at=now)
        except Exception:
            pass

        if "loggedIn: true" in combined or "logged in" in combined.lower():
            return LoginCheckResult(tool="claude", state="Ready", detail="Claude Code logged in", checked_at=now)
        if "not logged in" in combined.lower() or "login required" in combined.lower():
            return LoginCheckResult(tool="claude", state="Logged out", detail="Claude Code not logged in", checked_at=now)

        return LoginCheckResult(tool="claude", state="Logged out", detail="Claude Code auth status returned false or error", checked_at=now)

    # Free check: codex
    def _check_codex(self, repo: str) -> LoginCheckResult:
        now = datetime.now(timezone.utc).isoformat()
        if not resolve_bin("codex"):
            return LoginCheckResult(tool="codex", state="Not installed", detail="codex executable not found in PATH", checked_at=now)

        rc, out, err, timed_out = self._runner(["codex", "login", "status"], 30)
        if timed_out:
            return LoginCheckResult(tool="codex", state="Unknown (network)", detail="codex login status timed out", checked_at=now)

        combined = f"{out}\n{err}".lower()
        if "logged in" in combined and "not logged in" not in combined:
            return LoginCheckResult(tool="codex", state="Ready", detail="Codex (GPT) logged in", checked_at=now)
        if "not logged in" in combined or rc != 0:
            return LoginCheckResult(tool="codex", state="Logged out", detail="Codex is not logged in", checked_at=now)

        return LoginCheckResult(tool="codex", state="Unknown (network)", detail=f"codex login status: {err[:100]}", checked_at=now)

    # Tiny probe: gemini
    def _check_gemini(self, repo: str) -> LoginCheckResult:
        now = datetime.now(timezone.utc).isoformat()
        if not resolve_bin("gemini"):
            return LoginCheckResult(tool="gemini", state="Not installed", detail="gemini executable not found in PATH", checked_at=now)

        rc, out, err, timed_out = self._runner(["gemini", "-p", "Reply with OK", "--output-format", "json"], 30)
        if timed_out:
            return LoginCheckResult(tool="gemini", state="Unknown (network)", detail="gemini probe timed out", checked_at=now)

        combined = f"{out}\n{err}".lower()
        if rc == 0:
            return LoginCheckResult(tool="gemini", state="Ready", detail="gemini probe succeeded", checked_at=now)

        # Quota / rate limit error is a cooldown, NEVER logged out!
        if any(p in combined for p in ["rate limit", "quota", "429", "exhausted", "limit reached", "cooling down"]):
            reset = parse_reset_time(combined)
            back_str = reset[11:16] if reset and len(reset) >= 16 else "soon"
            state = f"Limit reached (back at {back_str})"
            if self._quota_ledger:
                self._quota_ledger.record_quota_error("gemini", "default", f"{out}\n{err}")
            return LoginCheckResult(tool="gemini", state=state, detail="gemini rate limit reached", back_at=reset, checked_at=now)

        if any(p in combined for p in ["auth", "login", "not authenticated", "unauthorized", "api key"]):
            return LoginCheckResult(tool="gemini", state="Logged out", detail="gemini not authenticated", checked_at=now)

        if any(p in combined for p in ["network", "dns", "connection", "resolve"]):
            return LoginCheckResult(tool="gemini", state="Unknown (network)", detail="gemini network failure", checked_at=now)

        return LoginCheckResult(tool="gemini", state="Logged out", detail=f"gemini probe failed: {err[:100]}", checked_at=now)

    # Tiny probe: agy
    def _check_agy(self, repo: str) -> LoginCheckResult:
        now = datetime.now(timezone.utc).isoformat()
        if not resolve_bin("agy"):
            return LoginCheckResult(tool="agy", state="Not installed", detail="agy executable not found in PATH", checked_at=now)

        rc, out, err, timed_out = self._runner(["agy", "-p", "Reply with OK", "--output-format", "json", "--print-timeout", "60s"], 60)
        if timed_out:
            return LoginCheckResult(tool="agy", state="Unknown (network)", detail="agy probe timed out", checked_at=now)

        combined = f"{out}\n{err}".lower()
        if rc == 0:
            return LoginCheckResult(tool="agy", state="Ready", detail="agy probe succeeded", checked_at=now)

        if any(p in combined for p in ["rate limit", "quota", "429", "exhausted", "limit reached", "cooling down"]):
            reset = parse_reset_time(combined)
            back_str = reset[11:16] if reset and len(reset) >= 16 else "soon"
            state = f"Limit reached (back at {back_str})"
            if self._quota_ledger:
                self._quota_ledger.record_quota_error("agy", "default", f"{out}\n{err}")
            return LoginCheckResult(tool="agy", state=state, detail="agy rate limit reached", back_at=reset, checked_at=now)

        if any(p in combined for p in ["auth", "login", "unauthorized", "not logged in"]):
            return LoginCheckResult(tool="agy", state="Logged out", detail="agy not logged in", checked_at=now)

        if any(p in combined for p in ["network", "dns", "connection"]):
            return LoginCheckResult(tool="agy", state="Unknown (network)", detail="agy network failure", checked_at=now)

        return LoginCheckResult(tool="agy", state="Logged out", detail=f"agy probe failed: {err[:100]}", checked_at=now)
