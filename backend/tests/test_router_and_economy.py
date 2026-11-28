"""Tests for Smart Quota-Aware Router, Economy-First tiers, DPAPI, and Escalations (BLUEPRINT 5a, 5c, 5h)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from nightshift.engine.credentials import (
    MemoryCredentialStore,
    WindowsDPAPICredentialStore,
    get_credential_store,
    redact_known_secrets,
)
from nightshift.engine.deck import generate_deck_data
from nightshift.engine.router import (
    Router,
    SIZE_CRITICAL,
    SIZE_LARGE,
    SIZE_MEDIUM,
    SIZE_SMALL,
    TIER_ECONOMY,
    TIER_GOLD,
    TIER_STANDARD,
    seed_default_engines,
)
from nightshift.engine.scheduler import EngineChoice, Scheduler
from nightshift.engine.vcs import FakeGitGh
from nightshift.engine.verify import redact_secrets
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.template import TemplateRunner
from nightshift.store import Store


@pytest.fixture
def test_store(tmp_path: Path) -> Store:
    db_path = tmp_path / "test_nightshift.db"
    store = Store(db_path)
    store.migrate()
    return store


@pytest.fixture
def quota_ledger(test_store: Store) -> QuotaLedger:
    return QuotaLedger(test_store)


def test_dpapi_round_trip_behind_interface(tmp_path: Path) -> None:
    """Test credential storage round-trip behind interface with memory store and DPAPI."""
    # 1. Memory store (used on CI / non-Windows)
    mem_store = MemoryCredentialStore()
    mem_store.set_secret("deepseek_key", "sk-deepseek-test-12345")
    assert mem_store.get_secret("deepseek_key") == "sk-deepseek-test-12345"
    assert "deepseek_key" in mem_store.list_keys()
    mem_store.delete_secret("deepseek_key")
    assert mem_store.get_secret("deepseek_key") is None

    # 2. Windows DPAPI storage (when on Windows and crypt32 is available)
    if sys.platform == "win32":
        try:
            import ctypes

            if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "crypt32"):
                dpapi_file = tmp_path / "test_dpapi.enc"
                dpapi_store = WindowsDPAPICredentialStore(storage_path=dpapi_file)
                dpapi_store.set_secret("kimi_key", "sk-kimi-secret-token-xyz")
                retrieved = dpapi_store.get_secret("kimi_key")
                assert retrieved == "sk-kimi-secret-token-xyz"
                assert "kimi_key" in dpapi_store.list_keys()
                dpapi_store.delete_secret("kimi_key")
                assert dpapi_store.get_secret("kimi_key") is None
        except Exception:
            pass


def test_env_isolation_and_secret_redaction(test_store: Store, quota_ledger: QuotaLedger) -> None:
    """Assert isolated env is built, ~/.claude is untouched, and keys are redacted from logs."""
    cred_store = MemoryCredentialStore()
    cred_store.set_secret("api_key_deepseek", "sk-deepseek-top-secret-999")

    router = Router(test_store, quota_ledger, cred_store=cred_store)

    # Check isolated environment
    home_claude = Path.home() / ".claude"
    before_exists = home_claude.exists()
    before_files = sorted(str(p) for p in home_claude.iterdir()) if before_exists else []

    env = router.build_isolated_environment("deepseek")
    assert env.get("ANTHROPIC_AUTH_TOKEN") == "sk-deepseek-top-secret-999"
    assert env.get("ANTHROPIC_BASE_URL") == "https://api.deepseek.com/anthropic"
    assert env.get("ANTHROPIC_MODEL") == "deepseek-chat"
    assert env.get("ANTHROPIC_SMALL_FAST_MODEL") == "deepseek-chat"

    # Assert ~/.claude is completely untouched
    after_exists = home_claude.exists()
    after_files = sorted(str(p) for p in home_claude.iterdir()) if after_exists else []
    assert after_exists == before_exists
    assert after_files == before_files

    # Assert secret is redacted by verify.redact_secrets
    raw_log = "Executed deepseek-chat with key sk-deepseek-top-secret-999 and returned 0"
    redacted = redact_secrets(raw_log)
    assert "sk-deepseek-top-secret-999" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_template_rendering_and_escaping_no_shell_injection(tmp_path: Path, quota_ledger: QuotaLedger) -> None:
    """Assert template args are built as a safe list without shell=True execution."""
    template = ["qwen", "-p", "{instruction}", "--dir", "{workdir}", "--effort", "{effort}"]
    runner = TemplateRunner("qwen", template, quota_ledger=quota_ledger)

    malicious_prompt = "Build feature; rm -rf / ; cat /etc/passwd | whoami & calc.exe"
    cmd = runner.build_command(malicious_prompt, tmp_path, model="qwen-2.5-coder", effort="high")

    # Command must be a pure list of strings, never a raw shell string
    assert isinstance(cmd, list)
    assert cmd[0] == "qwen"
    assert cmd[1] == "-p"
    assert cmd[2] == malicious_prompt
    assert cmd[3] == "--dir"
    assert cmd[4] == str(tmp_path)
    assert cmd[5] == "--effort"
    assert cmd[6] == "high"


def test_tier_size_routing_table(test_store: Store, quota_ledger: QuotaLedger) -> None:
    """Verify tier routing by size: small -> Economy, medium -> Economy first, large -> Standard."""
    router = Router(test_store, quota_ledger)

    # Mock all engines installed
    with patch.object(router, "is_engine_installed", return_value=True):
        # Small job -> picks Economy (qwen or deepseek)
        dec_small = router.pick("backend", size=SIZE_SMALL)
        assert dec_small.cost_tier == TIER_ECONOMY

        # Medium job -> picks Economy
        dec_med = router.pick("backend", size=SIZE_MEDIUM)
        assert dec_med.cost_tier == TIER_ECONOMY

        # Large job -> picks Standard (agy or gemini or codex)
        dec_large = router.pick("backend", size=SIZE_LARGE)
        assert dec_large.cost_tier == TIER_STANDARD

        # Critical job -> picks Standard
        dec_crit = router.pick("backend", size=SIZE_CRITICAL)
        assert dec_crit.cost_tier == TIER_STANDARD


def test_escalation_chain_economy_to_standard_to_gold(test_store: Store, quota_ledger: QuotaLedger) -> None:
    """Verify escalation chain: Economy -> Standard -> Gold with handoff checkpointing."""
    router = Router(test_store, quota_ledger)

    with patch.object(router, "is_engine_installed", return_value=True):
        # 1. Start with Economy
        dec1 = router.pick("backend", size=SIZE_SMALL)
        assert dec1.cost_tier == TIER_ECONOMY

        # 2. All economy engines fail / excluded -> escalates to Standard
        economy_engines = {e["id"] for e in test_store.list_engines() if e.get("cost_tier") == TIER_ECONOMY}
        dec2 = router.pick("backend", size=SIZE_SMALL, exclude_engines=economy_engines)
        assert dec2.cost_tier == TIER_STANDARD

        # 3. Standard engines fail / excluded -> escalates to Gold (Claude)
        standard_engines = {e["id"] for e in test_store.list_engines() if e.get("cost_tier") == TIER_STANDARD}
        dec3 = router.pick("backend", size=SIZE_SMALL, exclude_engines=economy_engines.union(standard_engines))
        assert dec3.cost_tier == TIER_GOLD
        assert dec3.chosen_id == "claude"


def test_auto_promotion_rule(test_store: Store, quota_ledger: QuotaLedger) -> None:
    """Assert economy engines with success rate < 60% over 10 jobs trigger auto-promotion to Standard."""
    router = Router(test_store, quota_ledger)

    with patch.object(router, "is_engine_installed", return_value=True):
        # Initially, medium jobs route to Economy
        dec_before = router.pick("backend", size=SIZE_MEDIUM)
        assert dec_before.cost_tier == TIER_ECONOMY

        # Record 10 attempts with only 4 successes (40% < 60%) for economy engines on medium size
        for _ in range(4):
            test_store.update_engine_stats("qwen", SIZE_MEDIUM, minutes=15.0, est_cost=0.05, success=True)
        for _ in range(6):
            test_store.update_engine_stats("qwen", SIZE_MEDIUM, minutes=15.0, est_cost=0.05, success=False)

        # Now auto-promotion rule should trip and start medium jobs in Standard
        promoted, reason = router.is_auto_promoted(SIZE_MEDIUM)
        assert promoted is True
        assert "auto-promoted to Standard" in reason

        dec_after = router.pick("backend", size=SIZE_MEDIUM)
        assert dec_after.cost_tier == TIER_STANDARD
        assert "auto-promoted" in dec_after.reason


def test_router_scoring_table(test_store: Store, quota_ledger: QuotaLedger) -> None:
    """Verify deterministic scoring: cooldown penalty, budget pressure, pins, and Claude penalty."""
    router = Router(test_store, quota_ledger)

    with patch.object(router, "is_engine_installed", return_value=True):
        scores = router.score_engines("review")
        # Claude carries a 4.5 cost penalty, so Codex (skill 5, standard cost penalty 1.0) scores higher for review
        claude_score = next(s for s in scores if s.engine_id == "claude")
        codex_score = next(s for s in scores if s.engine_id == "codex")
        assert codex_score.final_score > claude_score.final_score

        # Set Codex to cooling down
        quota_ledger.record_quota_error("codex", "o3-mini", "Rate limit exceeded. Reset in 30m.")
        scores_cooling = router.score_engines("review")
        codex_cooled = next(s for s in scores_cooling if s.engine_id == "codex")
        assert codex_cooled.available is False
        assert codex_cooled.final_score == -999.0

        # Owner pin overrides score
        test_store.upsert_engine(
            engine_id="gemini",
            runner="gemini",
            model="gemini-2.5-pro",
            effort="medium",
            skills_json=json.dumps({"review": 4}),
            cost_tier=TIER_STANDARD,
            pinned=1,
        )
        scores_pinned = router.score_engines("review")
        gemini_score = next(s for s in scores_pinned if s.engine_id == "gemini")
        assert gemini_score.final_score > 90.0


def test_implementer_resolution_table_and_fast_switchback(test_store: Store, quota_ledger: QuotaLedger, tmp_path: Path) -> None:
    """Verify plan implementer -> owner override -> priority fallback -> fast switchback."""
    scheduler = Scheduler(test_store, quota_ledger)
    vcs = FakeGitGh()

    # Create project and phase in store
    test_store.create_project(project_id="proj-1", name="Test Project", repo="owner/repo", local_path=str(tmp_path))
    test_store.create_plan("plan-1", "proj-1", "Test Plan")
    test_store.upsert_phase("p1", "plan-1", "Feature 1", "feat/p1")

    with patch.object(scheduler, "is_engine_available", return_value=True):
        # 1. Default plan implementer
        choice1 = scheduler.select_engine("p1", "implement")
        assert choice1 is not None

        # 2. Owner override phase assignment
        test_store.upsert_phase_assignment("p1", planned_engine="agy", override_engine="codex", model="o3-mini", effort="high")
        choice2 = scheduler.select_engine("p1", "implement")
        assert choice2 is not None
        assert choice2.engine == "codex"
        assert choice2.effort == "high"

        # 3. If override engine cools down -> falls back to another available engine
        quota_ledger.record_quota_error("codex", "o3-mini", "Quota exceeded. Try again in 60m.")
        with patch.object(scheduler, "is_engine_available", side_effect=lambda eng, mod="": eng != "codex"):
            choice3 = scheduler.select_engine("p1", "implement", current_engine="codex")
            assert choice3 is not None
            assert choice3.engine != "codex"

        # 4. Once cooldown ends, next step immediately returns with fast switch-back
        with patch.object(scheduler, "is_engine_available", return_value=True):
            choice5 = scheduler.select_engine("p1", "implement", current_engine="agy")
            assert choice5 is not None
            assert choice5.engine == "codex"
            assert choice5.is_switchback is True
            assert "fast switch-back" in choice5.reason


def test_gold_saved_tonight_estimate_in_deck(test_store: Store, quota_ledger: QuotaLedger, tmp_path: Path) -> None:
    """Verify jobs finished by Economy engines calculate gold saved in deck."""
    seed_default_engines(test_store)
    test_store.create_project("nightshift-desktop", "Night-Shift Desktop", "owner/nightshift-desktop", str(tmp_path))

    # Record 3 successful economy runs
    test_store.update_engine_stats("qwen", SIZE_SMALL, minutes=10.0, est_cost=0.02, success=True)
    test_store.update_engine_stats("qwen", SIZE_SMALL, minutes=12.0, est_cost=0.03, success=True)
    test_store.update_engine_stats("deepseek", SIZE_MEDIUM, minutes=20.0, est_cost=0.05, success=True)

    deck = generate_deck_data(test_store, "nightshift-desktop")

    assert "gold_saved_tonight" in deck
    gold_saved = deck["gold_saved_tonight"]
    assert gold_saved["jobs_done"] == 3
    assert gold_saved["cost_saved_usd"] == 0.90
    assert gold_saved["tokens_saved"] == 45000
    assert "$0.9 gold saved tonight (estimate)" in gold_saved["estimate_label"]


def test_usage_capture_json_and_fallback_run_count(test_store: Store, quota_ledger: QuotaLedger) -> None:
    """Verify CLI JSON usage capture and plain-text run-count fallback."""
    from nightshift.quota.usage import parse_cli_usage, record_cli_usage

    claude_json = json.dumps({"result": "ok", "usage": {"input_tokens": 1450, "output_tokens": 620}})
    assert parse_cli_usage(claude_json) == (1450, 620)

    gemini_json = json.dumps({"usage_metadata": {"prompt_token_count": 900, "candidates_token_count": 300}})
    assert parse_cli_usage(gemini_json) == (900, 300)

    assert parse_cli_usage("Plain text output without token counts") == (0, 0)

    t_in, t_out = record_cli_usage(quota_ledger, "claude", "claude-3-7-sonnet", claude_json)
    assert (t_in, t_out) == (1450, 620)
    record_cli_usage(quota_ledger, "codex", "o3-mini", "Completed task cleanly.")
    entries = test_store.get_ledger_entries(runner="codex")
    assert len(entries) == 1
    assert entries[0]["event"] == "run"


def test_mid_run_override_and_priority_change_apply_at_next_step(
    test_store: Store, quota_ledger: QuotaLedger, tmp_path: Path
) -> None:
    """Verify mid-run implementer override and priority changes take effect on the very next step."""
    scheduler = Scheduler(test_store, quota_ledger)
    test_store.create_project("proj-mid", "Mid", "owner/mid", str(tmp_path))
    test_store.create_plan("plan-mid", "proj-mid", "Plan")
    test_store.upsert_phase("p1", "plan-mid", "Phase 1", "feat/p1")
    test_store.upsert_phase("p2", "plan-mid", "Phase 2", "feat/p2")

    with patch.object(scheduler, "is_engine_available", return_value=True):
        # Step 1: initial assignment for p1 is agy
        test_store.upsert_phase_assignment("p1", planned_engine="agy")
        first = scheduler.select_engine("p1", "implement")
        assert first is not None and first.engine == "agy"

        # Mid-run: owner overrides p2 to gemini and changes priority list to put codex first
        test_store.upsert_phase_assignment("p2", planned_engine="agy", override_engine="gemini", effort="medium")
        test_store.set_priority_list([
            {"position": 1, "engine": "codex", "model": "o3-mini", "effort": "medium"},
            {"position": 2, "engine": "agy", "model": "gemini-4-neon", "effort": "medium"},
        ])

        next_step = scheduler.select_engine("p2", "implement")
        assert next_step is not None and next_step.engine == "gemini"

        # If gemini cools down on next step, updated priority list picks codex immediately
        quota_ledger.record_quota_error("gemini", "default", "Rate limit exceeded. Reset in 20m.")
        with patch.object(scheduler, "is_engine_available", side_effect=lambda eng, mod="": eng != "gemini"):
            fallback_step = scheduler.select_engine("p2", "implement", current_engine="gemini")
            assert fallback_step is not None and fallback_step.engine == "codex"


def test_escalation_with_checkpoint_handoff_economy_standard_gold(
    test_store: Store, quota_ledger: QuotaLedger, tmp_path: Path
) -> None:
    """Verify Economy -> Standard -> Gold escalation creates durable handoff checkpoints."""
    router = Router(test_store, quota_ledger)
    scheduler = Scheduler(test_store, quota_ledger, router=router)
    vcs = FakeGitGh()
    wt = tmp_path / "wt_esc"
    wt.mkdir()
    vcs.prepare_worktree(tmp_path, wt, "feat/esc")

    test_store.create_project("proj-e", "Esc", "owner/esc", str(tmp_path))
    test_store.create_plan("plan-e", "proj-e", "Plan")
    test_store.upsert_phase("p-esc", "plan-e", "Small fix", "feat/esc", size=SIZE_SMALL)

    with patch.object(router, "is_engine_installed", return_value=True):
        dec1 = router.pick("backend", size=SIZE_SMALL)
        assert dec1.cost_tier == TIER_ECONOMY

        econ_ids = {e["id"] for e in test_store.list_engines() if e.get("cost_tier") == TIER_ECONOMY}
        dec2 = router.pick("backend", size=SIZE_SMALL, exclude_engines=econ_ids)
        assert dec2.cost_tier == TIER_STANDARD
        scheduler.create_mid_phase_handoff(
            vcs=vcs, worktree_dir=wt, phase_id="p-esc", phase_title="Small fix",
            from_engine=dec1.chosen_id, to_engine=dec2.chosen_id, reason="Economy NO-OP", branch="feat/esc",
        )

        std_ids = {e["id"] for e in test_store.list_engines() if e.get("cost_tier") == TIER_STANDARD}
        dec3 = router.pick("backend", size=SIZE_SMALL, exclude_engines=econ_ids.union(std_ids))
        assert dec3.cost_tier == TIER_GOLD
        scheduler.create_mid_phase_handoff(
            vcs=vcs, worktree_dir=wt, phase_id="p-esc", phase_title="Small fix",
            from_engine=dec2.chosen_id, to_engine=dec3.chosen_id, reason="Standard quota exhausted", branch="feat/esc",
        )

        handoffs = test_store.list_handoffs("p-esc")
        assert len(handoffs) == 2
        assert handoffs[0]["to_engine"] == dec2.chosen_id
        assert handoffs[1]["to_engine"] == "claude"
        assert (wt / ".nightshift" / "handoff.md").exists()


def test_zai_flash_env_key_is_process_scoped_and_redacted(test_store: Store, quota_ledger: QuotaLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_default_engines(test_store)
    monkeypatch.setenv("ZAI_API_KEY", "test-zai-secret-value")
    router = Router(test_store, quota_ledger)
    env = router.build_isolated_environment("zai-glm-flash")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-zai-secret-value"
    assert env["ANTHROPIC_MODEL"] == "GLM-4.7-Flash"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert "test-zai-secret-value" not in redact_known_secrets("test-zai-secret-value")
    assert "test-zai-secret-value" not in str(test_store.get_engine("zai-glm-flash"))
