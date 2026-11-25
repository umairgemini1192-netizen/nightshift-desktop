"""Tests for implementer selection, mid-phase handoff, fast switch-back, runaway limits, and ETA (BLUEPRINT 5a, 5c, 5d, 5h)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightshift.engine.scheduler import CircuitBreaker, SameErrorDetector, Scheduler
from nightshift.engine.vcs import FakeGitGh
from nightshift.quota.ledger import QuotaLedger
from nightshift.store import Store


def test_implementer_selection_hierarchy(tmp_path: Path) -> None:
    db = tmp_path / "scheduler.db"
    with Store(db) as store:
        store.migrate()
        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)

        store.create_project("proj-1", "Test", "owner/repo", str(tmp_path))
        store.create_plan("plan-1", "proj-1", "Plan")
        store.upsert_phase("p1", "plan-1", "Phase 1", "feat/p1")

        # 1. Default fallback: priority list top item (agy)
        c1 = sched.select_engine("p1")
        assert c1 is not None
        assert c1.engine == "agy"
        assert c1.effort == "medium"

        # 2. Planned engine set to gemini
        store.upsert_phase_assignment("p1", planned_engine="gemini")
        c2 = sched.select_engine("p1")
        assert c2 is not None
        assert c2.engine == "gemini"

        # 3. Owner override set to codex supersedes planned
        store.upsert_phase_assignment("p1", planned_engine="gemini", override_engine="codex")
        c3 = sched.select_engine("p1")
        assert c3 is not None
        assert c3.engine == "codex"


def test_table_driven_handoff_and_fast_switchback(tmp_path: Path) -> None:
    """Table test per BLUEPRINT 5c & prompt:

    Claude exhausted mid-phase -> Antigravity continues from checkpoint;
    Claude back -> takes over at next fix round;
    All engines exhausted -> wait for earliest reset without burning attempts.
    """
    db = tmp_path / "handoff_table.db"
    with Store(db) as store:
        store.migrate()
        quota = QuotaLedger(store, default_cooldown_seconds=1800)
        vcs = FakeGitGh()
        sched = Scheduler(store, quota)

        store.create_project("proj-1", "Test", "owner/repo", str(tmp_path))
        store.create_plan("plan-1", "proj-1", "Plan")
        store.upsert_phase("p1", "plan-1", "Phase 1", "feat/p1")

        wt_dir = tmp_path / "worktree_p1"
        wt_dir.mkdir(parents=True, exist_ok=True)
        vcs.prepare_worktree(tmp_path, wt_dir, "feat/p1")

        # Step 1: Owner assigns Claude as preferred engine
        store.upsert_phase_assignment("p1", override_engine="claude")
        choice1 = sched.select_engine("p1")
        assert choice1 is not None
        assert choice1.engine == "claude"

        # Step 2: Claude exhausted mid-phase (quota limit) -> trigger mid-phase handoff
        quota.record_quota_error("claude", "default", "Claude quota exceeded. Resets in 30 minutes.")
        assert quota.is_cooling_down("claude")

        # Select next available engine -> agy (top of priority list)
        choice2 = sched.select_engine("p1", current_engine="claude")
        assert choice2 is not None
        assert choice2.engine == "agy"

        # Mid-phase handoff execution: creates WIP commit, writes handoff.md, records in store
        checkpoint_sha = sched.create_mid_phase_handoff(
            vcs=vcs,
            worktree_dir=wt_dir,
            phase_id="p1",
            phase_title="Phase 1",
            from_engine="claude",
            to_engine=choice2.engine,
            reason="Claude quota limit reached",
            remaining_acceptance_tests="Verify test_store and test_engine pass",
            last_error="429 Rate limit exceeded",
        )
        assert checkpoint_sha is not None
        handoff_file = wt_dir / ".nightshift" / "handoff.md"
        assert handoff_file.exists()
        assert "**From Engine**: claude" in handoff_file.read_text(encoding="utf-8")
        assert "**To Engine**: agy" in handoff_file.read_text(encoding="utf-8")

        handoff_records = store.list_handoffs("p1")
        assert len(handoff_records) == 1
        assert handoff_records[0]["from_engine"] == "claude"
        assert handoff_records[0]["to_engine"] == "agy"

        # Step 3: Fast switch-back!
        # When Claude's cooldown clears, next invocation boundary hands back to Claude
        # Simulate cooldown cleared by clearing ledger cooldown
        store.connection.execute("DELETE FROM runner_ledger WHERE runner = 'claude'")
        assert not quota.is_cooling_down("claude")

        switchback_choice = sched.select_engine("p1", current_engine="agy")
        assert switchback_choice is not None
        assert switchback_choice.engine == "claude"
        assert switchback_choice.is_switchback is True
        assert "fast switch-back" in switchback_choice.reason

        # Step 4: All engines exhausted
        for eng in ["agy", "gemini", "codex", "claude"]:
            quota.record_quota_error(eng, "default", f"{eng} rate limit reached")

        exhausted_choice = sched.select_engine("p1")
        assert exhausted_choice is None  # None available -> engine pauses/waits for reset without burning attempts
        assert quota.earliest_cooldown_expiry() is not None


def test_same_error_detector_rotation_and_parking() -> None:
    detector = SameErrorDetector()

    # Different errors from same engine -> no rotation
    rot1, park1 = detector.record_error("p1", "agy", "SyntaxError: invalid syntax on line 12")
    assert not rot1 and not park1

    rot2, park2 = detector.record_error("p1", "agy", "ImportError: cannot import name 'foo'")
    assert not rot2 and not park2

    # Identical error twice in a row from same engine -> rotate engine!
    rot3, park3 = detector.record_error("p1", "agy", "NullPointerException at line 45")
    assert not rot3 and not park3
    rot4, park4 = detector.record_error("p1", "agy", "NullPointerException at line 45")
    assert rot4 is True
    assert park4 is False

    # Identical error across 3 distinct engines -> park phase!
    detector.record_error("p1", "gemini", "NullPointerException at line 45")
    rot_p, park_p = detector.record_error("p1", "codex", "NullPointerException at line 45")
    assert rot_p is True
    assert park_p is True


def test_circuit_breaker() -> None:
    breaker = CircuitBreaker(threshold=3)
    assert not breaker.is_tripped

    # 1 parked phase
    assert not breaker.record_phase_result(parked=True)
    assert breaker.consecutive_parked_count == 1

    # 1 success resets counter
    assert not breaker.record_phase_result(parked=False)
    assert breaker.consecutive_parked_count == 0

    # 3 parked in a row trips the breaker
    breaker.record_phase_result(parked=True)
    breaker.record_phase_result(parked=True)
    tripped = breaker.record_phase_result(parked=True)
    assert tripped is True
    assert breaker.is_tripped is True


def test_eta_estimator(tmp_path: Path) -> None:
    db = tmp_path / "eta.db"
    with Store(db) as store:
        store.migrate()
        quota = QuotaLedger(store)
        sched = Scheduler(store, quota)

        # Before recorded durations: fallback estimate
        snap_initial = sched.calculate_eta(pending_phase_count=2, active_step_kind="implement", active_runner="agy")
        assert snap_initial.is_estimate is True
        assert "(estimate)" in snap_initial.formatted

        # Record 4 measured durations
        for sec in [1200.0, 1300.0, 1250.0, 1350.0]:
            store.record_duration("implement", "agy", sec)

        snap_measured = sched.calculate_eta(pending_phase_count=1, active_step_kind="implement", active_runner="agy")
        assert snap_measured.is_estimate is False
        assert "(estimate from past runs)" in snap_measured.formatted
        assert snap_measured.seconds_remaining > 0
