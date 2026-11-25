"""Tests for quota ledger, cooldowns, reset parsing, and runner CLI command builders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nightshift.quota.ledger import QuotaLedger, parse_reset_time
from nightshift.runners import (
    AgyRunner,
    ClaudeRunner,
    CodexRunner,
    FakeRunner,
    GeminiRunner,
    RunnerResult,
    TemplateRunner,
)
from nightshift.store import Store


def test_parse_reset_time_formats() -> None:
    base = datetime(2026, 11, 25, 2, 0, 0, tzinfo=timezone.utc)

    # ISO format
    iso_res = parse_reset_time("Usage limit hit. Reset at 2026-11-25T04:15:00Z.", base_time=base)
    assert iso_res is not None
    assert "04:15:00" in iso_res

    # Delta minutes
    delta_res = parse_reset_time("Rate limit exceeded. Try again in 45 minutes.", base_time=base)
    assert delta_res is not None
    dt = datetime.fromisoformat(delta_res)
    assert (dt - base).total_seconds() == 45 * 60

    # Delta seconds
    delta_s = parse_reset_time("Please retry after 120 seconds.", base_time=base)
    assert delta_s is not None
    dt_s = datetime.fromisoformat(delta_s)
    assert (dt_s - base).total_seconds() == 120

    # Clock time HH:MM (later same day)
    clock_res = parse_reset_time("Limit reached. Back at 03:30.", base_time=base)
    assert clock_res is not None
    assert "03:30:00" in clock_res

    # Clock time HH:MM earlier in day -> rolls to next day
    clock_rollover = parse_reset_time("Back at 01:00.", base_time=base)
    assert clock_rollover is not None
    dt_roll = datetime.fromisoformat(clock_rollover)
    assert dt_roll > base


def test_quota_ledger_cooldown_tracking_and_expiry(tmp_path: Path) -> None:
    db = tmp_path / "quota.db"
    with Store(db) as store:
        store.migrate()
        ledger = QuotaLedger(store, default_cooldown_seconds=1800)

        assert not ledger.is_cooling_down("gemini")
        assert ledger.get_cooldown_remaining("gemini") == 0.0

        # Record quota error
        reset_str = ledger.record_quota_error("gemini", "gemini-2.5-pro", "Rate limit exceeded. Try again in 30 minutes.")
        assert ledger.is_cooling_down("gemini")
        remaining = ledger.get_cooldown_remaining("gemini")
        assert 1700 <= remaining <= 1800

        earliest = ledger.earliest_cooldown_expiry()
        assert earliest is not None
        assert earliest.isoformat() == reset_str

        # Other runner is not cooling down
        assert not ledger.is_cooling_down("claude")


def test_quota_ledger_budget_caps(tmp_path: Path) -> None:
    db = tmp_path / "quota_budget.db"
    with Store(db) as store:
        store.migrate()
        ledger = QuotaLedger(store)

        budget = {"max_runs_today": 2, "max_tokens_today": 1000}
        assert ledger.check_budget_cap("claude", budget)

        ledger.record_usage("claude", "claude-3-5-sonnet", tokens_in=200, tokens_out=300)
        assert ledger.check_budget_cap("claude", budget)

        ledger.record_usage("claude", "claude-3-5-sonnet", tokens_in=300, tokens_out=400)
        # 2 runs used, max is 2 -> cap reached
        assert not ledger.check_budget_cap("claude", budget)


def test_proven_runner_command_lines(tmp_path: Path) -> None:
    prompt = "Test prompt"

    # 1. Antigravity: agy -p <prompt> --dangerously-skip-permissions
    agy = AgyRunner()
    agy_cmd = agy.build_command(prompt, model="gemini-2.5-pro", effort="medium")
    assert agy_cmd == ["agy", "-p", prompt, "--dangerously-skip-permissions", "--model", "gemini-2.5-pro", "--effort", "medium"]

    # 2. Gemini: gemini -p <prompt> --approval-mode=yolo --skip-trust
    gemini = GeminiRunner()
    gemini_cmd = gemini.build_command(prompt, model="gemini-2.5-flash")
    assert gemini_cmd == ["gemini", "-p", prompt, "--approval-mode=yolo", "--skip-trust", "--model", "gemini-2.5-flash"]

    # 3. Codex: codex exec --sandbox workspace-write --approve-for-me <prompt>
    codex = CodexRunner()
    codex_cmd = codex.build_command(prompt, model="o3-mini")
    assert codex_cmd == ["codex", "exec", "--sandbox", "workspace-write", "--approve-for-me", prompt, "--model", "o3-mini"]

    # 4. Claude: claude -p <prompt> --permission-mode bypassPermissions --output-format text
    claude = ClaudeRunner()
    claude_cmd = claude.build_command(prompt, model="claude-3-7-sonnet", effort="high")
    assert claude_cmd == [
        "claude", "-p", prompt, "--permission-mode", "bypassPermissions", "--output-format", "text",
        "--model", "claude-3-7-sonnet", "--effort", "high"
    ]


def test_template_runner_command_generation(tmp_path: Path) -> None:
    template = ["custom-ai", "--run", "{instruction}", "--model", "{model}", "--dir", "{workdir}"]
    runner = TemplateRunner("custom", template)
    cmd = runner.build_command("Do task", tmp_path, model="custom-v1", effort="medium")
    assert cmd == ["custom-ai", "--run", "Do task", "--model", "custom-v1", "--dir", str(tmp_path)]


@pytest.mark.asyncio
async def test_fake_runner_modes_and_call_tracking(tmp_path: Path) -> None:
    called_prompts: list[str] = []

    def on_call(p: str, wd: Path, m: str | None, eff: str) -> None:
        called_prompts.append(p)

    runner = FakeRunner(
        name="fake_test",
        responses=[
            RunnerResult(rc=0, stdout="First ok", stderr=""),
            RunnerResult(rc=1, stdout="", stderr="Rate limit hit", quota_error=True),
        ],
        on_call=on_call,
    )

    res1 = await runner.run("prompt 1", tmp_path)
    assert res1.rc == 0
    assert res1.stdout == "First ok"

    res2 = await runner.run("prompt 2", tmp_path)
    assert res2.rc == 1
    assert res2.quota_error is True

    assert called_prompts == ["prompt 1", "prompt 2"]
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_agy_success_counts_towards_daily_budget(tmp_path, monkeypatch):
    with Store(tmp_path / "budget.db") as store:
        store.migrate()
        ledger = QuotaLedger(store)
        monkeypatch.setattr("nightshift.runners.agy.run_subprocess_with_timeout", lambda *a, **k: (0, "done", "", False))
        await AgyRunner(ledger).run("work", tmp_path)
        assert not ledger.check_budget_cap("agy", {"max_runs_today": 1})

def test_budget_percentage_preserves_caps(tmp_path):
    with Store(tmp_path / "percentage.db") as store:
        store.migrate()
        ledger = QuotaLedger(store)
        budget = {"max_runs_today": 10, "max_tokens_today": 1000, "weekly_percent": 20}
        assert ledger.check_budget_cap("codex", budget)
        ledger.record_usage("codex", "", tokens_in=100)
        assert ledger.check_budget_cap("codex", budget)
        ledger.record_usage("codex", "", tokens_in=100)
        assert not ledger.check_budget_cap("codex", budget)
        assert not ledger.check_budget_cap("codex", {"weekly_percent": 0})
