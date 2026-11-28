"""Phase 8f tests: triage role on small_job_ladder, cost-per-accepted-change ledger,
benchmark graduation/demotion, hard budgets, risk-based router, and pipeline_ledger module.

Covers AGENT_PIPELINE.md section 12.1 (Phase 8f) and Amendments I & J.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nightshift.bridge import BridgeAPI
from nightshift.engine.deck import generate_deck_data
from nightshift.engine.pipeline_economics import (
    PipelineEconomics,
    compute_entry_total_cost_usd_est,
    is_free_only_engine,
)
from nightshift.engine.pipeline_triage import (
    MAX_TRIAGE_LINES,
    deterministic_triage_fallback,
    parse_triage_output,
    run_triage_stage,
)
from nightshift.engine.router import Router
from nightshift.events import Event, EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.quota.ledger import QuotaLedger
from nightshift.runners.fake import FakeRunner
from nightshift.store import Store


def _make_store_with_task(
    tmp_path: Path,
    *,
    task_id: str = "t-8f-1",
    risk_class: str = "business_logic",
    budget: dict | None = None,
) -> tuple[Store, dict]:
    store = Store(tmp_path / "nightshift.db")
    store.migrate()
    store.create_project("proj-1", "NightShift", "owner/nightshift-desktop", str(tmp_path))
    store.create_plan("plan-1", "proj-1", "Phase 8f Plan")
    store.upsert_phase("p8f", "plan-1", "Triage & Economics", "feat/p8f")
    task, _ = store.create_task_with_contract(
        task_id=task_id,
        phase_id="p8f",
        plan_id="plan-1",
        title="Test task",
        branch=f"feat/{task_id}",
        risk_class=risk_class,
        budget=budget,
    )
    return store, task


def _make_router(store: Store, events: EventBus | None = None) -> Router:
    quota = QuotaLedger(store)
    router = Router(store, quota, events=events)
    # Mock CLI presence so tests never depend on host PATH
    router.is_engine_installed = lambda _runner: True  # type: ignore[method-assign]
    return router


# ---------------------------------------------------------------------------
# 1. Stage 4 TRIAGE on small_job_ladder (<= 10 lines, cause + file:line + fix)
# ---------------------------------------------------------------------------


def test_triage_output_strictly_bounded_and_structured(tmp_path: Path) -> None:
    store, _ = _make_store_with_task(tmp_path, task_id="t-triage-1")
    # Graduate qwen for triage/business_logic so Economy routing allows it
    store.upsert_model_score(
        engine_id="qwen",
        model="qwen-2.5-coder-32b",
        role="triage",
        risk_class="business_logic",
        sample_tasks=10,
        accepted_tasks=10,
        total_attempts=10,
        regression_count=0,
        avg_retries=0.0,
        avg_turnaround_min=0.5,
        total_cost_usd_est=0.0,
        cost_per_accepted_usd_est=0.0,
        benchmark_graduated=True,
    )
    triage_json = json.dumps(
        {
            "root_cause": "AssertionError in test_router expected 0.85 but got 0.75",
            "file_line": "backend/tests/test_router.py:42",
            "suggested_fix": "Use neutral bootstrap prior equal to risk threshold when sample_tasks < 5.",
            "notes": [f"extra note {i}" for i in range(15)],  # intentionally > 10 lines
        }
    )
    runners = {
        "qwen": FakeRunner.success(name="qwen", stdout=triage_json),
    }
    summary = asyncio.run(
        run_triage_stage(
            failing_log="FAILED backend/tests/test_router.py:42 AssertionError: 0.75 < 0.85",
            workdir=tmp_path,
            runners=runners,
            store=store,
            task_id="t-triage-1",
            risk_class="business_logic",
            job_name="desktop-backend",
        )
    )
    assert not summary.used_fallback
    assert summary.engine_id == "qwen"
    assert summary.line_count <= MAX_TRIAGE_LINES
    assert "backend/tests/test_router.py:42" in summary.file_line
    assert "AssertionError" in summary.root_cause
    assert "bootstrap prior" in summary.suggested_fix

    attempts = store.list_task_attempts(task_id="t-triage-1", stage="TRIAGE")
    assert len(attempts) == 1
    assert attempts[0]["role"] == "triage"
    assert attempts[0]["engine_id"] == "qwen"

    ledger_rows = store.list_pipeline_ledger(task_id="t-triage-1", role="triage")
    assert len(ledger_rows) == 1
    # Successful triage output is recorded as attempt-level "ok", never task-level "accepted"
    assert ledger_rows[0]["outcome"] == "ok"


def test_triage_deterministic_fallback_and_secret_redaction() -> None:
    raw_log = (
        'File "backend/nightshift/engine/router.py", line 118, in pick\n'
        "ValueError: invalid token ghp_1234567890abcdefghijklmnopqrstuvwxyz1234\n"
    )
    summary = deterministic_triage_fallback(raw_log, job_name="desktop-backend")
    assert summary.used_fallback
    assert summary.line_count <= MAX_TRIAGE_LINES
    assert summary.file_line == "backend/nightshift/engine/router.py:118"
    assert "ghp_1234567890" not in summary.render()
    assert parse_triage_output("not valid json at all") is None


# ---------------------------------------------------------------------------
# 2. Benchmark graduation & auto-demotion (Amendment I / AGENT_PIPELINE §6.3)
# ---------------------------------------------------------------------------


def test_economy_graduation_and_auto_demotion(tmp_path: Path) -> None:
    store = Store(tmp_path / "nightshift.db")
    store.migrate()
    store.create_project("proj-1", "NightShift", "owner/nightshift-desktop", str(tmp_path))
    store.create_plan("plan-1", "proj-1", "Benchmarks")
    store.upsert_phase("p8f", "plan-1", "Benchmarks", "feat/p8f")
    events = EventBus(store)
    econ = PipelineEconomics(store, events)
    _make_router(store, events)  # seeds default engines

    # Seed incumbent Standard engine score ($0.30/accepted)
    store.upsert_model_score(
        engine_id="agy-gemini-4-neon",
        model="gemini-4-neon",
        role="coder",
        risk_class="docs_format",
        sample_tasks=10,
        accepted_tasks=9,
        total_attempts=11,
        regression_count=0,
        avg_retries=0.1,
        avg_turnaround_min=3.0,
        total_cost_usd_est=2.70,
        cost_per_accepted_usd_est=0.30,
        benchmark_graduated=True,
    )

    # Record 9 qualifying Economy tasks -> still benchmarking (not yet 10)
    for i in range(1, 10):
        tid = f"econ-t{i}"
        store.create_task_with_contract(task_id=tid, phase_id="p8f", plan_id="plan-1", title=tid, branch=f"feat/{tid}", risk_class="docs_format")
        econ.record_ledger_event(
            task_id=tid,
            role="coder",
            engine_id="qwen",
            model="qwen-2.5-coder-32b",
            risk_class="docs_format",
            tokens_in=5000,
            tokens_out=2000,
            est_token_cost_usd=0.0,
            ci_minutes=2.0,
            artifact_mb=1.0,
            wall_seconds=60.0,
            outcome="accepted",
        )
    score_9 = store.get_model_score("qwen", "qwen-2.5-coder-32b", "coder", "docs_format")
    assert score_9 is not None
    assert score_9["sample_tasks"] == 9
    assert score_9["benchmark_graduated"] == 0

    # 10th qualifying task -> graduates!
    store.create_task_with_contract(task_id="econ-t10", phase_id="p8f", plan_id="plan-1", title="econ-t10", branch="feat/econ-t10", risk_class="docs_format")
    econ.record_ledger_event(
        task_id="econ-t10",
        role="coder",
        engine_id="qwen",
        model="qwen-2.5-coder-32b",
        risk_class="docs_format",
        tokens_in=5000,
        tokens_out=2000,
        est_token_cost_usd=0.0,
        ci_minutes=2.0,
        artifact_mb=1.0,
        wall_seconds=60.0,
        outcome="accepted",
    )
    score_10 = store.get_model_score("qwen", "qwen-2.5-coder-32b", "coder", "docs_format")
    assert score_10 is not None
    assert score_10["sample_tasks"] == 10
    assert score_10["benchmark_graduated"] == 1
    assert score_10["cost_per_accepted_usd_est"] < 0.30

    # Regression spike (> 10% in rolling 10 tasks): 2 reverted tasks trigger auto-demotion
    emitted: list[Event] = []
    events.subscribe("pipeline.model_demoted", emitted.append)
    for j in range(11, 13):
        tid = f"econ-t{j}"
        store.create_task_with_contract(task_id=tid, phase_id="p8f", plan_id="plan-1", title=tid, branch=f"feat/{tid}", risk_class="docs_format")
        econ.record_ledger_event(
            task_id=tid,
            role="coder",
            engine_id="qwen",
            model="qwen-2.5-coder-32b",
            risk_class="docs_format",
            est_token_cost_usd=0.0,
            ci_minutes=2.0,
            outcome="reverted",
        )
    score_after = store.get_model_score("qwen", "qwen-2.5-coder-32b", "coder", "docs_format")
    assert score_after is not None
    assert score_after["benchmark_graduated"] == 0
    assert len(emitted) == 1
    assert "Auto-demoted qwen" in emitted[0].plain_text


# ---------------------------------------------------------------------------
# 3. Risk-based Router: hard safeguards before owner overrides, bootstrap & fallback
# ---------------------------------------------------------------------------


def test_router_hard_safeguards_checked_before_owner_overrides(tmp_path: Path) -> None:
    store, _ = _make_store_with_task(tmp_path, task_id="t-route-1")
    router = _make_router(store)

    # 3a. Provider independence: google contributed -> owner override 'gemini' MUST be BLOCKED
    dec_indep = router.route_pipeline_task(
        role="reviewer",
        risk_class="business_logic",
        contributing_providers={"google"},
        owner_override="gemini",
        free_only_mode=True,
    )
    assert dec_indep.blocked
    assert "independence" in dec_indep.reason.lower()

    # 3b. High-risk strong-model rule: Economy 'qwen' cannot run high_risk even if graduated & overridden
    store.upsert_model_score(
        engine_id="qwen",
        model="qwen-2.5-coder-32b",
        role="coder",
        risk_class="high_risk",
        sample_tasks=12,
        accepted_tasks=12,
        total_attempts=12,
        regression_count=0,
        avg_retries=0.0,
        avg_turnaround_min=1.0,
        total_cost_usd_est=0.0,
        cost_per_accepted_usd_est=0.0,
        benchmark_graduated=True,
    )
    dec_high = router.route_pipeline_task(
        role="coder",
        risk_class="high_risk",
        owner_override="qwen",
        free_only_mode=True,
    )
    assert dec_high.blocked
    assert "standard or gold" in dec_high.reason.lower()

    # 3c. Economy graduation gate: ungraduated 'qwen' blocked on docs_format even with override
    dec_ungrad = router.route_pipeline_task(
        role="coder",
        risk_class="docs_format",
        owner_override="qwen",
        free_only_mode=True,
    )
    assert dec_ungrad.blocked
    assert "benchmark-graduated" in dec_ungrad.reason.lower()

    # 3d. free_only_mode blocks paid engine 'claude' even with owner override
    dec_paid = router.route_pipeline_task(
        role="coder",
        risk_class="high_risk",
        owner_override="claude",
        free_only_mode=True,
    )
    assert dec_paid.blocked
    assert "free_only_mode" in dec_paid.reason

    # 3e. Eligible owner override succeeds (codex reviewing google's work)
    dec_ok = router.route_pipeline_task(
        role="reviewer",
        risk_class="business_logic",
        contributing_providers={"google"},
        owner_override="codex",
        free_only_mode=True,
    )
    assert not dec_ok.blocked
    assert dec_ok.chosen_id == "codex"
    assert "Owner pinned codex" in dec_ok.reason


def test_router_cold_start_bootstrap_and_threshold_fallback(tmp_path: Path) -> None:
    store, _ = _make_store_with_task(tmp_path, task_id="t-boot-1")
    events = EventBus(store)
    warnings: list[Event] = []
    events.subscribe("router.threshold_fallback", warnings.append)
    router = _make_router(store, events)

    # Cold start (0 samples): Standard engines use neutral bootstrap prior -> never deadlocks
    dec_cold = router.route_pipeline_task(
        role="coder",
        risk_class="business_logic",
        free_only_mode=True,
    )
    assert not dec_cold.blocked
    assert dec_cold.cost_tier == "standard"

    # Drop ALL Standard free engines below threshold (0.85 for business_logic, 0.90 for high_risk)
    for eng in store.list_engines():
        if eng["cost_tier"] == "standard":
            for rc in ("business_logic", "high_risk"):
                store.upsert_model_score(
                    engine_id=eng["id"],
                    model=eng["model"],
                    role="coder",
                    risk_class=rc,
                    sample_tasks=10,
                    accepted_tasks=6 if eng["id"] != "codex" else 7,
                    total_attempts=12,
                    regression_count=0,
                    avg_retries=0.2,
                    avg_turnaround_min=2.0,
                    total_cost_usd_est=1.0,
                    cost_per_accepted_usd_est=0.15,
                    benchmark_graduated=True,
                )

    # business_logic falls back to highest effective_rate Standard engine (codex, 70%) + warning event
    dec_fb = router.route_pipeline_task(
        role="coder",
        risk_class="business_logic",
        free_only_mode=True,
    )
    assert not dec_fb.blocked
    assert dec_fb.chosen_id == "codex"
    assert dec_fb.warning
    assert len(warnings) == 1

    # high_risk MUST return BLOCKED when no Standard/Gold engine meets threshold
    dec_hr = router.route_pipeline_task(
        role="coder",
        risk_class="high_risk",
        free_only_mode=True,
    )
    assert dec_hr.blocked
    assert "reliability threshold" in dec_hr.reason.lower()


# ---------------------------------------------------------------------------
# 4. Hard budgets (task / night / month) & free_only_mode (§7.1)
# ---------------------------------------------------------------------------


def test_hard_budgets_stop_cleanly_across_all_three_scopes(tmp_path: Path) -> None:
    store, _ = _make_store_with_task(
        tmp_path,
        task_id="t-budget-1",
        budget={"max_tokens": 10000, "max_usd_est": 0.50, "max_attempts": 2, "max_wall_minutes": 30},
    )
    econ = PipelineEconomics(store)

    # Free-only mode blocks paid engine immediately
    assert is_free_only_engine("agy", runner="agy", cost_tier="standard")
    assert is_free_only_engine("qwen", runner="qwen", cost_tier="economy")
    assert not is_free_only_engine("claude", runner="claude", cost_tier="gold")
    assert not is_free_only_engine("deepseek", runner="claude-compatible", cost_tier="economy")

    chk_free = econ.check_hard_budgets(task_id="t-budget-1", engine_id="claude", runner="claude", cost_tier="gold")
    assert not chk_free.allowed
    assert "free-only mode" in chk_free.reason.lower()

    # Disable free-only mode and test task / night / month caps
    store.set_setting("pipeline.freeOnlyMode", "0")
    store.set_setting("budget.maxNightUsdEst", "1.00")
    store.set_setting("budget.maxMonthUsdEst", "1.50")

    # Exceed task token cap & accumulate paid API spend via claude
    econ.record_ledger_event(
        task_id="t-budget-1",
        role="coder",
        engine_id="claude",
        model="claude-3-7-sonnet",
        risk_class="business_logic",
        tokens_in=8000,
        tokens_out=3000,
        est_token_cost_usd=0.10,
        outcome="retry",
        recompute=False,
    )
    chk_task = econ.check_hard_budgets(task_id="t-budget-1", engine_id="claude", runner="claude", cost_tier="gold")
    assert not chk_task.allowed
    assert chk_task.scope == "task"

    # Exceed night paid USD cap
    chk_night = econ.check_hard_budgets(engine_id="claude", runner="claude", cost_tier="gold", projected_usd_est=0.95)
    assert not chk_night.allowed
    assert chk_night.scope == "night"

    # Raise night cap above month cap -> month paid USD cap stops cleanly
    store.set_setting("budget.maxNightUsdEst", "5.00")
    chk_month = econ.check_hard_budgets(engine_id="claude", runner="claude", cost_tier="gold", projected_usd_est=1.45)
    assert not chk_month.allowed
    assert chk_month.scope == "month"


# ---------------------------------------------------------------------------
# 5. Pipeline Ledger backend module, maturity labels (Amendment J), and deck
# ---------------------------------------------------------------------------


def test_pipeline_ledger_bridge_overview_and_maturity_promotion(tmp_path: Path) -> None:
    store = Store(tmp_path / "nightshift.db")
    store.migrate()
    store.create_project("proj-1", "NightShift", "owner/nightshift-desktop", str(tmp_path))
    store.create_plan("plan-1", "proj-1", "Phase 8f Plan")
    store.upsert_phase("p8f", "plan-1", "Triage & Economics", "feat/p8f")
    events = EventBus(store)
    registry = ModuleRegistry()
    registry.discover()
    assert "pipeline_ledger" in registry.list_ids()

    bridge = BridgeAPI(store=store, events=events, registry=registry)
    econ = PipelineEconomics(store, events)

    # Before 5 accepted tasks: maturity is Integration-tested
    res0 = bridge.call("pipeline_ledger.getOverview", {"projectId": "proj-1"})
    assert res0["ok"]
    assert all(s["maturity"] == "Integration-tested" for s in res0["data"]["stageMaturity"])

    # Record 5 clean accepted tasks -> Amendment J promotes to Production-proven
    for i in range(1, 6):
        tid = f"prod-t{i}"
        store.create_task_with_contract(task_id=tid, phase_id="p8f", plan_id="plan-1", title=tid, branch=f"feat/{tid}")
        econ.record_ledger_event(
            task_id=tid,
            role="coder",
            engine_id="qwen",
            model="qwen-2.5-coder-32b",
            risk_class="business_logic",
            tokens_in=4000,
            tokens_out=1500,
            est_token_cost_usd=0.0,
            ci_minutes=2.0,
            artifact_mb=2.0,
            wall_seconds=90.0,
            outcome="accepted",
        )

    res5 = bridge.call("pipeline_ledger.getOverview", {"projectId": "proj-1"})
    assert res5["ok"]
    data = res5["data"]
    assert data["summary"]["acceptedChanges30d"] == 5
    assert data["summary"]["goldSavedTasks"] == 5
    assert all(s["maturity"] == "Production-proven" for s in data["stageMaturity"])
    assert compute_entry_total_cost_usd_est({"est_token_cost_usd": 0.1, "ci_minutes": 5.0, "artifact_mb": 10.0}) == pytest.approx(0.145)

    # Mission Control deck includes budget_tonight free-only display line (§11.1)
    deck = generate_deck_data(store, project_id="proj-1")
    assert "budget_tonight" in deck
    assert "free-only mode" in deck["budget_tonight"]["displayLine"]


# ---------------------------------------------------------------------------
# 6. P1 Review Regression Tests (safeguards, triage eligibility, paid spend,
#    attempt vs accepted separation, and production ledger wiring)
# ---------------------------------------------------------------------------


def test_triage_enforces_eligibility_on_every_candidate_and_falls_back_deterministically(tmp_path: Path) -> None:
    store, _ = _make_store_with_task(tmp_path, task_id="t-triage-elig", risk_class="high_risk")
    router = _make_router(store)
    # Even if qwen is graduated for triage/high_risk, high_risk forbids Economy engines
    store.upsert_model_score(
        engine_id="qwen",
        model="qwen-2.5-coder-32b",
        role="triage",
        risk_class="high_risk",
        sample_tasks=12,
        accepted_tasks=12,
        total_attempts=12,
        regression_count=0,
        avg_retries=0.0,
        avg_turnaround_min=0.5,
        total_cost_usd_est=0.0,
        cost_per_accepted_usd_est=0.0,
        benchmark_graduated=True,
    )
    qwen = FakeRunner.success(name="qwen", stdout='{"root_cause":"x","file_line":"a.py:1","suggested_fix":"y"}')
    gemini = FakeRunner.success(name="gemini", stdout="not json")
    # Disable gemini so no eligible candidate can succeed
    gem_row = store.get_engine("gemini")
    assert gem_row is not None
    store.upsert_engine(
        engine_id="gemini",
        runner=gem_row["runner"],
        model=gem_row["model"],
        effort=gem_row["effort"],
        skills_json=gem_row["skills_json"],
        cost_tier=gem_row["cost_tier"],
        budget_json=gem_row["budget_json"],
        enabled=0,
    )

    summary = asyncio.run(
        run_triage_stage(
            failing_log="FAILED app.py:12 AssertionError: boom",
            workdir=tmp_path,
            runners={"qwen": qwen, "gemini": gemini},
            store=store,
            router=router,
            task_id="t-triage-elig",
            risk_class="high_risk",
        )
    )
    assert summary.used_fallback
    assert not qwen.calls
    assert not gemini.calls

    # Also verify ungraduated qwen on business_logic without an explicit router is NEVER run
    store2, _ = _make_store_with_task(tmp_path / "sub", task_id="t-triage-ungrad", risk_class="business_logic")
    qwen2 = FakeRunner.success(name="qwen", stdout='{"root_cause":"x","file_line":"a.py:1","suggested_fix":"y"}')
    summary2 = asyncio.run(
        run_triage_stage(
            failing_log="FAILED app.py:12 AssertionError: boom",
            workdir=tmp_path,
            runners={"qwen": qwen2},
            store=store2,
            task_id="t-triage-ungrad",
            risk_class="business_logic",
        )
    )
    assert summary2.used_fallback
    assert not qwen2.calls


def test_triage_success_does_not_count_as_accepted_change_or_promote_maturity(tmp_path: Path) -> None:
    store = Store(tmp_path / "triage_maturity.db")
    store.migrate()
    store.create_project("proj-1", "NightShift", "owner/nightshift-desktop", str(tmp_path))
    store.create_plan("plan-1", "proj-1", "Triage Maturity")
    store.upsert_phase("p8f", "plan-1", "Triage Maturity", "feat/p8f")
    router = _make_router(store)
    econ = PipelineEconomics(store)

    triage_json = json.dumps(
        {
            "root_cause": "AssertionError in test_foo",
            "file_line": "backend/tests/test_foo.py:10",
            "suggested_fix": "Fix assertion",
        }
    )
    runners = {"gemini": FakeRunner.success(name="gemini", stdout=triage_json)}

    for i in range(1, 6):
        tid = f"queued-t{i}"
        store.create_task_with_contract(task_id=tid, phase_id="p8f", plan_id="plan-1", title=tid, branch=f"feat/{tid}")
        res = asyncio.run(
            run_triage_stage(
                failing_log="FAILED test_foo.py:10 AssertionError",
                workdir=tmp_path,
                runners=runners,
                store=store,
                router=router,
                task_id=tid,
                risk_class="business_logic",
            )
        )
        assert not res.used_fallback

    summary = econ.get_30d_summary(project_id="proj-1")
    assert summary["acceptedChanges30d"] == 0
    assert all(st["maturity"] == "Integration-tested" for st in econ.get_stage_maturity())


def test_free_engine_cost_estimates_do_not_exhaust_free_only_paid_cap(tmp_path: Path) -> None:
    store, _ = _make_store_with_task(tmp_path, task_id="t-free-cap")
    econ = PipelineEconomics(store)
    assert econ.is_free_only_mode() is True

    # Record a Gemini triage invocation with positive estimated token cost + CI/storage
    econ.record_ledger_event(
        task_id="t-free-cap",
        role="triage",
        engine_id="gemini",
        model="gemini-2.5-pro",
        risk_class="business_logic",
        tokens_in=20000,
        tokens_out=5000,
        ci_minutes=5.0,
        artifact_mb=10.0,
        wall_seconds=30.0,
        outcome="ok",
    )

    tonight = econ.get_tonight_spend_summary()
    assert tonight["paidSpentUsdEst"] == 0.0
    assert tonight["economicCostUsdEst"] > 0.0
    assert tonight["displayLine"] == "$0.00 / $0.00 (free-only mode)"

    # Free engines (qwen, agy, gemini, codex) remain allowed across night and month scopes
    for eng_id, runner, tier in (
        ("qwen", "qwen", "economy"),
        ("agy-gemini-4-neon", "agy", "standard"),
        ("gemini", "gemini", "standard"),
        ("codex", "codex", "standard"),
    ):
        chk = econ.check_hard_budgets(
            task_id="t-free-cap",
            role="coder",
            engine_id=eng_id,
            runner=runner,
            cost_tier=tier,
        )
        assert chk.allowed, f"Free engine {eng_id} was wrongly blocked: {chk.reason}"

    # Paid engine remains blocked under free-only mode
    chk_paid = econ.check_hard_budgets(task_id="t-free-cap", role="coder", engine_id="claude", runner="claude", cost_tier="gold")
    assert not chk_paid.allowed


def test_production_dispatch_boundaries_enforce_safeguards_and_record_economics(tmp_path: Path) -> None:
    from nightshift.engine.scheduler import Scheduler

    store, _ = _make_store_with_task(tmp_path, task_id="t-disp-1", risk_class="business_logic")
    quota = QuotaLedger(store)
    runners = {
        "claude": FakeRunner.success(name="claude"),
        "qwen": FakeRunner.success(name="qwen"),
        "agy": FakeRunner.success(name="agy"),
    }
    sched = Scheduler(store, quota, runners=runners)

    # Owner override to paid claude MUST be blocked under default free_only_mode
    store.upsert_phase_assignment("p8f", override_engine="claude")
    choice_paid = sched.select_engine(
        phase_id="p8f",
        step_kind="implement",
        task_id="t-disp-1",
        risk_class="business_logic",
        role="coder",
        allowed_engines=set(runners.keys()),
        enforce_pipeline_safeguards=True,
    )
    assert choice_paid is None

    # Owner override to ungraduated Economy qwen MUST be blocked
    store.upsert_phase_assignment("p8f", override_engine="qwen")
    choice_ungrad = sched.select_engine(
        phase_id="p8f",
        step_kind="implement",
        task_id="t-disp-1",
        risk_class="business_logic",
        role="coder",
        allowed_engines=set(runners.keys()),
        enforce_pipeline_safeguards=True,
    )
    assert choice_ungrad is None

    # Auto assignment chooses eligible free Standard engine (agy)
    store.upsert_phase_assignment("p8f", override_engine="auto")
    choice_ok = sched.select_engine(
        phase_id="p8f",
        step_kind="implement",
        task_id="t-disp-1",
        risk_class="business_logic",
        role="coder",
        allowed_engines=set(runners.keys()),
        enforce_pipeline_safeguards=True,
    )
    assert choice_ok is not None
    assert choice_ok.engine == "agy"

    # Terminal outcome propagation updates model_scores for attempt-level "ok" rows
    econ = PipelineEconomics(store)
    econ.record_ledger_event(
        task_id="t-disp-1",
        role="coder",
        engine_id="agy",
        model="gemini-4-neon",
        risk_class="business_logic",
        tokens_in=1000,
        tokens_out=500,
        wall_seconds=12.0,
        outcome="ok",
    )
    assert econ.get_30d_summary()["acceptedChanges30d"] == 0
    econ.record_task_terminal_outcome(task_id="t-disp-1", outcome="accepted", risk_class="business_logic", ci_minutes=1.5)
    assert econ.get_30d_summary()["acceptedChanges30d"] == 1
    score = store.get_model_score("agy", "gemini-4-neon", "coder", "business_logic")
    assert score is not None
    assert score["accepted_tasks"] == 1

    # Reverted terminal outcome (e.g. rollback) flips task into regression_count
    econ.record_task_terminal_outcome(task_id="t-disp-1", outcome="reverted", risk_class="business_logic")
    score_rev = store.get_model_score("agy", "gemini-4-neon", "coder", "business_logic")
    assert score_rev is not None
    assert score_rev["accepted_tasks"] == 0
    assert score_rev["regression_count"] == 1


@pytest.mark.asyncio
async def test_lazy_runners_stay_eligible_for_subsequent_phase_and_failover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 regression: lazily cached runners in self.runners must not become an allowlist."""
    from nightshift.engine.logins import LoginHealthChecker
    from nightshift.engine.planner import PhaseDefinition
    from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
    from nightshift.engine.vcs import FakeGitGh
    from nightshift.events import EventBus
    from nightshift.runners.base import RunnerResult

    store = Store(tmp_path / "nightshift.db")
    store.migrate()
    events = EventBus(store)
    quota = QuotaLedger(store)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    vcs = FakeGitGh()
    vcs.branches["feat/p1"] = "sha-p1"
    vcs.branches["feat/p2"] = "sha-p2"
    logins = LoginHealthChecker(store=store, quota_ledger=quota)

    # Normal desktop startup: runners=None (empty cache, lazily populated)
    engine = EngineStateMachine(
        store=store,
        event_bus=events,
        quota_ledger=quota,
        vcs=vcs,
        runners=None,
        login_checker=logins,
    )
    # Pretend all CLIs are installed and logged in
    engine.scheduler.router.is_engine_installed = lambda _r: True  # type: ignore[method-assign]
    monkeypatch.setattr(engine.scheduler, "is_engine_available", lambda _e, _m="": True)

    invoked_runners: list[str] = []

    class StubTemplateRunner:
        def __init__(self, name: str, *_args: Any, **_kwargs: Any) -> None:
            self.name = name

        async def run(self, _instruction: str, _workdir: Path, **_kwargs: Any) -> RunnerResult:
            invoked_runners.append(self.name)
            return RunnerResult(rc=1, stdout="", stderr="simulated failure")

    monkeypatch.setattr("nightshift.engine.state_machine.TemplateRunner", StubTemplateRunner)

    project = ProjectConfig(
        id="proj-lazy",
        name="LazyTest",
        repo="owner/lazy",
        local_path=repo_dir,
    )
    store.create_project("proj-lazy", "LazyTest", "owner/lazy", str(repo_dir))
    store.create_plan("plan-lazy", "proj-lazy", "Lazy Plan")
    store.upsert_phase("p1", "plan-lazy", "Phase 1", "feat/p1")
    store.upsert_phase("p2", "plan-lazy", "Phase 2", "feat/p2")
    store.upsert_phase_assignment("p1", override_engine="agy")
    store.upsert_phase_assignment("p2", override_engine="codex")

    # Phase 1: agy fails on attempt 1 -> should fail over to another catalogue engine (not blocked by cache)
    p1 = PhaseDefinition(id="p1", title="Phase 1", branch="feat/p1", prompt="Do p1", auto_merge=False)
    engine.scheduler.max_implement_attempts = 2
    await engine._execute_phase(project, p1, "plan-lazy", "main")
    assert "agy" in engine.runners
    assert invoked_runners[0] == "agy"
    assert len(invoked_runners) == 2
    assert invoked_runners[1] != "agy"

    # Phase 2: explicitly pinned to codex -> must still be eligible even though agy is already cached
    invoked_runners.clear()
    engine.scheduler.max_implement_attempts = 1
    p2 = PhaseDefinition(id="p2", title="Phase 2", branch="feat/p2", prompt="Do p2", auto_merge=False)
    await engine._execute_phase(project, p2, "plan-lazy", "main")
    assert invoked_runners == ["codex"]
    assert "codex" in engine.runners


@pytest.mark.asyncio
async def test_task_attempt_and_fix_counters_enforce_contract_budgets(tmp_path: Path) -> None:
    """P1 regression: attempt_count, ci_fix_rounds, and review_fix_rounds advance and enforce contract limits."""
    from nightshift.engine.ci import CiPipelineManager
    from nightshift.engine.logins import LoginHealthChecker
    from nightshift.engine.planner import PhaseDefinition
    from nightshift.engine.scheduler import Scheduler
    from nightshift.engine.state_machine import EngineStateMachine, ProjectConfig
    from nightshift.engine.vcs import FakeGitGh
    from nightshift.engine.verify import RiskAssessment
    from nightshift.events import EventBus
    from nightshift.runners.base import RunnerResult

    # 1. Coder max_attempts=1 stops before 2nd attempt even when scheduler allows 3
    store, task_coder = _make_store_with_task(
        tmp_path,
        task_id="t-cap-coder",
        budget={"max_tokens": 120000, "max_usd_est": 0.50, "max_attempts": 1, "max_ci_fix_rounds": 1, "max_review_fix_rounds": 1},
    )
    events = EventBus(store)
    quota = QuotaLedger(store)
    repo_dir = tmp_path / "repo_coder"
    repo_dir.mkdir()
    vcs = FakeGitGh()
    vcs.branches["feat/t-cap-coder"] = "sha1"
    logins = LoginHealthChecker(store=store, quota_ledger=quota)

    coder_calls = 0

    class CountingFailRunner(FakeRunner):
        async def run(self, instruction: str, workdir: Path, **kwargs: Any) -> RunnerResult:
            nonlocal coder_calls
            coder_calls += 1
            return RunnerResult(rc=1, stdout="", stderr="coder fail")

    engine = EngineStateMachine(
        store=store,
        event_bus=events,
        quota_ledger=quota,
        vcs=vcs,
        runners={"agy": CountingFailRunner("agy"), "codex": CountingFailRunner("codex")},
        login_checker=logins,
    )
    engine.scheduler.is_engine_available = lambda _e, _m="": True  # type: ignore[method-assign]
    engine.scheduler.max_implement_attempts = 3
    project = ProjectConfig(id="proj-1", name="NightShift", repo="owner/nightshift-desktop", local_path=repo_dir)
    phase = PhaseDefinition(id="p8f", title="Capped Coder", branch="feat/t-cap-coder", prompt="Fix bug", auto_merge=False)
    res = await engine._execute_phase(project, phase, "plan-1", "main")
    assert res["status"] == "PARKED"
    assert coder_calls == 1
    assert store.get_task(str(task_coder["id"]))["attempt_count"] == 1

    # 2. CI-fix max_ci_fix_rounds=1 stops before 2nd CI-fix round even when max_retries=4
    ci_dir = tmp_path / "ci_sub"
    ci_dir.mkdir()
    store_ci, task_ci = _make_store_with_task(
        ci_dir,
        task_id="t-cap-ci",
        budget={"max_tokens": 120000, "max_usd_est": 0.50, "max_attempts": 3, "max_ci_fix_rounds": 1, "max_review_fix_rounds": 1},
    )
    contract_ci = store_ci.get_task_contract(str(task_ci["contract_id"]))
    wt_ci = tmp_path / "ci_wt"
    wt_ci.mkdir()
    vcs_ci = FakeGitGh()
    vcs_ci.add_pr("feat/ci", number=10, head_sha="sha-ci")
    vcs_ci.ci_results[10] = [False, False, False, False, False]
    vcs_ci.local_head_sha = lambda _d: "sha-ci"  # type: ignore[method-assign]
    ci_fix_calls = 0

    class CountingFixRunner(FakeRunner):
        async def run(self, instruction: str, workdir: Path, **kwargs: Any) -> RunnerResult:
            nonlocal ci_fix_calls
            if "fix the CI failure" in instruction:
                ci_fix_calls += 1
            return RunnerResult(rc=0, stdout="fixed", stderr="")

    runners_ci = {"agy": CountingFixRunner("agy"), "codex": CountingFixRunner("codex")}
    sched_ci = Scheduler(store_ci, QuotaLedger(store_ci), runners=runners_ci)
    sched_ci.is_engine_available = lambda _e, _m="": True  # type: ignore[method-assign]
    ci_mgr = CiPipelineManager(vcs=vcs_ci, store=store_ci, scheduler=sched_ci, runners=runners_ci)
    ci_res = await ci_mgr.watch_and_heal(
        repo="owner/nightshift-desktop",
        local_repo=wt_ci,
        worktree_dir=wt_ci,
        phase_id="p8f",
        phase_title="CI Cap",
        branch="feat/ci",
        pr_number=10,
        initial_engine="agy",
        risk=RiskAssessment(level="low", reasons=[]),
        max_retries=4,
        task_contract=contract_ci,
        require_independent_reviewer=False,
    )
    assert not ci_res.passed
    assert ci_fix_calls == 1
    assert store_ci.get_task("t-cap-ci")["ci_fix_rounds"] == 1

    # 3. Review-fix max_review_fix_rounds=1 stops before 2nd review-fix round even when max_review_fix_rounds arg=3
    rev_dir = tmp_path / "rev_sub"
    rev_dir.mkdir()
    store_rev, task_rev = _make_store_with_task(
        rev_dir,
        task_id="t-cap-rev",
        budget={"max_tokens": 120000, "max_usd_est": 0.50, "max_attempts": 3, "max_ci_fix_rounds": 3, "max_review_fix_rounds": 1},
    )
    contract_rev = store_rev.get_task_contract(str(task_rev["contract_id"]))
    wt_rev = tmp_path / "rev_wt"
    wt_rev.mkdir()
    (wt_rev / "app.py").write_text("x = 1\n", encoding="utf-8")
    vcs_rev = FakeGitGh()
    vcs_rev.add_pr("feat/rev", number=11, head_sha="sha-rev")
    vcs_rev.ci_results[11] = [True, True, True]
    vcs_rev.local_head_sha = lambda _d: "sha-rev"  # type: ignore[method-assign]
    vcs_rev.get_diff = lambda _d: "diff --git a/app.py b/app.py\n+x = 1\n"  # type: ignore[method-assign]
    vcs_rev.list_changed_files = lambda _d: ["app.py"]  # type: ignore[method-assign]

    review_fix_calls = 0

    class CountingRevFixRunner(FakeRunner):
        async def run(self, instruction: str, workdir: Path, **kwargs: Any) -> RunnerResult:
            nonlocal review_fix_calls
            review_fix_calls += 1
            return RunnerResult(rc=0, stdout="fixed review", stderr="")

    class ChangesReviewer(FakeRunner):
        async def run(self, instruction: str, workdir: Path, **kwargs: Any) -> RunnerResult:
            payload = json.dumps({
                "verdict": "CHANGES",
                "summary": "Still broken",
                "findings": [
                    {
                        "priority": "P1",
                        "kind": "defect",
                        "file": "app.py",
                        "line": 1,
                        "message": "Bug remains",
                    }
                ],
            })
            return RunnerResult(rc=0, stdout=payload, stderr="")

    runners_rev = {"agy": CountingRevFixRunner("agy"), "codex": ChangesReviewer("codex")}
    sched_rev = Scheduler(store_rev, QuotaLedger(store_rev), runners=runners_rev)
    sched_rev.is_engine_available = lambda _e, _m="": True  # type: ignore[method-assign]
    ci_mgr_rev = CiPipelineManager(vcs=vcs_rev, store=store_rev, scheduler=sched_rev, runners=runners_rev)
    rev_res = await ci_mgr_rev.watch_and_heal(
        repo="owner/nightshift-desktop",
        local_repo=wt_rev,
        worktree_dir=wt_rev,
        phase_id="p8f",
        phase_title="Rev Cap",
        branch="feat/rev",
        pr_number=11,
        initial_engine="agy",
        risk=RiskAssessment(level="low", reasons=[]),
        max_retries=4,
        max_review_fix_rounds=3,
        task_contract=contract_rev,
        require_independent_reviewer=True,
    )
    assert not rev_res.passed
    assert review_fix_calls == 1
    assert store_rev.get_task("t-cap-rev")["review_fix_rounds"] == 1
