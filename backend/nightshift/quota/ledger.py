"""Quota ledger: parses rate-limit errors, tracks cooldowns, and estimates per-run usage.

Implements BLUEPRINT sections 5a, 5c, 5h and 6.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from nightshift.store import Store

QUOTA_PATTERNS = [
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "resource_exhausted",
    "too many requests",
    "429",
    "overloaded",
    "capacity",
    "cooling down",
    "try again later",
    "exhausted your",
    "limit reached",
    "usage limit",
    "resets at",
    "reset at",
    "back at",
    "credit balance",
    "insufficient_quota",
    "quota error",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_reset_time(text: str, base_time: datetime | None = None) -> str | None:
    """Extract a reset timestamp from quota error message text if present."""
    now = base_time or _utc_now()
    blob = text.strip()

    # Match ISO timestamp e.g. 2026-11-25T05:30:00 or 2026-11-25 05:30:00
    iso_match = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", blob)
    if iso_match:
        try:
            val = iso_match.group(1).replace(" ", "T")
            if not val.endswith("Z") and "+" not in val and "-" not in val[10:]:
                val += "Z"
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass

    # Match "resets in X min/minutes/s/seconds" or "try again in Xm/s/h"
    delta_match = re.search(r"(?:resets? in|try again in|retry after|wait)\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)?", blob, re.IGNORECASE)
    if delta_match:
        qty = int(delta_match.group(1))
        unit = (delta_match.group(2) or "s").lower()
        if unit.startswith("h"):
            target = now + timedelta(hours=qty)
        elif unit.startswith("m"):
            target = now + timedelta(minutes=qty)
        else:
            target = now + timedelta(seconds=qty)
        return target.isoformat()

    # Match "reset at HH:MM" or "back at HH:MM" or "back HH:MM"
    time_match = re.search(r"(?:resets? at|reset at|back at|back)\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", blob, re.IGNORECASE)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        sec = int(time_match.group(3) or 0)
        target = now.replace(hour=hour, minute=minute, second=sec, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()

    return None


class QuotaLedger:
    """Manages AI engine cooldowns, rate-limit error detection, and budget tracking."""

    def __init__(self, store: Store, default_cooldown_seconds: int = 3600) -> None:
        self._store = store
        self._default_cooldown_seconds = default_cooldown_seconds

    @property
    def store(self) -> Store:
        return self._store

    def is_quota_or_transient_error(
        self,
        rc: int,
        stdout: str,
        stderr: str,
        custom_patterns: list[str] | None = None,
    ) -> bool:
        """Return True if the runner output indicates a quota, rate limit, or transient failure."""
        if rc == 127:
            return True
        blob = f"{stdout}\n{stderr}".lower()
        patterns = list(QUOTA_PATTERNS) + (custom_patterns or [])
        return any(p.lower() in blob for p in patterns)

    def record_quota_error(
        self,
        runner: str,
        model: str,
        error_text: str,
        cooldown_seconds: int | None = None,
    ) -> str:
        """Parse reset time or apply cooldown, persist in runner_ledger, and return reset_at."""
        now = _utc_now()
        parsed_reset = parse_reset_time(error_text, base_time=now)
        if parsed_reset:
            reset_at = parsed_reset
        else:
            sec = cooldown_seconds if cooldown_seconds is not None else self._default_cooldown_seconds
            reset_at = (now + timedelta(seconds=sec)).isoformat()

        self._store.record_ledger_entry(
            runner=runner,
            model=model,
            event="quota_error",
            reset_at=reset_at,
        )
        return reset_at

    def record_usage(
        self,
        runner: str,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        event: str = "run",
    ) -> None:
        """Record token usage for an engine invocation."""
        self._store.record_ledger_entry(
            runner=runner,
            model=model,
            event=event,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def is_cooling_down(self, runner: str, model: str = "") -> bool:
        """Check if the given runner (and optional model) is currently cooling down."""
        reset_str = self._store.get_active_cooldown(runner, model)
        if not reset_str:
            return False
        try:
            reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
            return reset_dt > _utc_now()
        except Exception:
            return False

    def get_cooldown_remaining(self, runner: str, model: str = "") -> float:
        """Return remaining cooldown in seconds, or 0.0 if not cooling down."""
        reset_str = self._store.get_active_cooldown(runner, model)
        if not reset_str:
            return 0.0
        try:
            reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
            remaining = (reset_dt - _utc_now()).total_seconds()
            return max(0.0, remaining)
        except Exception:
            return 0.0

    def get_reset_at(self, runner: str, model: str = "") -> datetime | None:
        """Return the datetime when cooldown expires, or None."""
        reset_str = self._store.get_active_cooldown(runner, model)
        if not reset_str:
            return None
        try:
            return datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def earliest_cooldown_expiry(self) -> datetime | None:
        """Return the earliest expiration time among all currently active cooldowns."""
        now = _utc_now()
        entries = self._store.get_ledger_entries(limit=100)
        active_resets: list[datetime] = []
        for e in entries:
            reset_str = e.get("reset_at")
            if reset_str:
                try:
                    dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                    if dt > now:
                        active_resets.append(dt)
                except Exception:
                    continue
        if not active_resets:
            return None
        return min(active_resets)

    def check_budget_cap(self, runner: str, budget: str | dict[str, Any]) -> bool:
        """Return True if engine is within budget cap; False if budget cap is exceeded."""
        if isinstance(budget, str):
            try:
                b_data = json.loads(budget) if budget.strip() else {}
            except Exception:
                b_data = {}
        else:
            b_data = budget or {}

        max_runs_today = b_data.get("max_runs_today")
        max_tokens_today = b_data.get("max_tokens_today")

        percent = max(0.0, min(100.0, float(b_data.get("weekly_percent", 100)))) / 100.0
        if percent == 0:
            return False
        if max_runs_today is None and max_tokens_today is None:
            return True

        today_start = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        entries = self._store.get_ledger_entries(runner=runner, limit=500)
        today_entries = [e for e in entries if str(e.get("created_at", "")) >= today_start]

        if max_runs_today is not None and len(today_entries) >= float(max_runs_today) * percent:
            return False

        if max_tokens_today is not None:
            total_tokens = sum(int(e.get("tokens_in", 0)) + int(e.get("tokens_out", 0)) for e in today_entries)
            if total_tokens >= float(max_tokens_today) * percent:
                return False

        return True
