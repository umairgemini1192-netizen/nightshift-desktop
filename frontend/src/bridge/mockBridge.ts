import { SCENARIO_FIXTURES } from "./fixtures";
import type {
  AppSnapshot,
  Bridge,
  BridgeEventCallback,
  LoginCheckItem,
  PipelineState,
  ScenarioName,
  TokenOptimizerState,
} from "./types";

export interface StreamEvent {
  kind: string;
  plain_text: string;
  level?: "info" | "warn" | "error";
  payload?: Record<string, unknown>;
}

export const REALISTIC_STREAMS: Record<string, StreamEvent[]> = {
  "all-good": [
    { kind: "phase.started", plain_text: "Started Phase 1: Environment & auth verification on branch feat/p1." },
    { kind: "runner.started", plain_text: "Antigravity assigned to implement Phase 1." },
    { kind: "ci.started", plain_text: "Continuous integration checks running on PR #10." },
    { kind: "ci.passed", plain_text: "Continuous integration checks passed on PR #10." },
    { kind: "phase.merged", plain_text: "Phase 1 merged cleanly to main." },
    { kind: "run.finished", plain_text: "Overnight run finished cleanly: all planned phases merged." },
  ],
  handoff: [
    { kind: "runner.started", plain_text: "Claude Code started Phase 2 on branch feat/p2." },
    { kind: "runner.exhausted", plain_text: "Claude Code hit weekly limit at 01:42." },
    { kind: "checkpoint.created", plain_text: "Created WIP checkpoint on branch feat/p2." },
    { kind: "handoff.started", plain_text: "Claude Code hit limit at 01:42; Antigravity continued Phase 2 from Claude's checkpoint." },
    { kind: "switchback.armed", plain_text: "Claude came back from cooldown and took over Phase 2 from Antigravity." },
  ],
  "ci-healing": [
    { kind: "ci.failed", plain_text: "Continuous integration checks failed; starting automatic fix round." },
    { kind: "ci.fixing", plain_text: "Codex fixing failing tests from CI log (attempt 1 of 4 with fresh eyes rotation)." },
    { kind: "ci.fixed", plain_text: "Continuous integration fixer pushed a new commit to resolve test failures." },
    { kind: "ci.passed", plain_text: "Continuous integration checks passed after self-healing round 1." },
  ],
  "parked-stacked": [
    { kind: "review.changes", plain_text: "Risk-based review reported open P1 security finding." },
    { kind: "phase.parked", plain_text: "Phase 2 parked: login change failed review twice. Stacked dependent phases will build on top." },
    { kind: "stacked.started", plain_text: "Phase 3 continuing as stacked PR on top of Phase 2 branch." },
  ],
  finished: [
    { kind: "phase.merged", plain_text: "Phase 5 merged cleanly to main." },
    { kind: "release.ready", plain_text: "Release preview.44 published with SHA256SUMS." },
    { kind: "run.finished", plain_text: "Overnight run finished: all 5 phases merged successfully." },
  ],
};

export class MockBridge implements Bridge {
  private scenario: ScenarioName;
  private snapshot: AppSnapshot;
  private listeners = new Map<string, Set<BridgeEventCallback>>();
  private stopArmed = false;
  private autostartEnabled = false;
  private firstRunDismissed = false;
  private phoneStatus: {
    enabled: boolean;
    url: string;
    hasSecret: boolean;
    lastResult: string;
    lastResultAt: string;
  };
  private tokenOptimizerState: TokenOptimizerState;
  private pipelineState: PipelineState;

  constructor(scenario: ScenarioName = "idle") {
    this.scenario = scenario;
    this.snapshot = structuredClone(SCENARIO_FIXTURES[scenario] ?? SCENARIO_FIXTURES.idle);
    this.stopArmed = Boolean(this.snapshot.stopArmed);
    const isFailure = scenario === "failure";
    const isRunning = scenario === "running";
    this.tokenOptimizerState = {
      tools: [
        {
          name: "ccusage",
          displayName: "ccusage",
          role: "Per-engine, per-model token usage from local logs (--offline --json)",
          version: "15.2.0",
          artifact: "ccusage-15.2.0.tgz",
          sha256: "8a4f2c91b7e630d5f1a9c4e28b3d7061f5e9a2c8d4b7013f6e8a9c2d5b1f4e70",
          license: "MIT",
          runners: ["agy", "gemini", "codex", "claude"],
          catalogueOnly: false,
          isolationGateRequired: false,
          isolationPassed: false,
          isolationMethod: "none",
          sha256Verified: true,
          enabled: isRunning || isFailure,
          autoDisabled: false,
          status: isRunning || isFailure ? "enabled" : "disabled",
          warning: isFailure
            ? "ccusage did not report Antigravity usage; falling back to CLI JSON usage and run counts."
            : "",
          tokensSavedTonight: 0,
          estimateLabel: "~0 tokens saved (estimate)",
          abAction: "keep_measuring",
          abReason: "Feeds the quota ledger and A/B measurements.",
        },
        {
          name: "repomix",
          displayName: "repomix",
          role: "Compact repo map (--compress) injected into each phase prompt",
          version: "0.3.9",
          artifact: "repomix-0.3.9.tgz",
          sha256: "3c7e1a58d9f420b6e8a1c3f57d2b9048e6c1a7f3b5d9024e8a6c1f3d7b2e5a91",
          license: "MIT",
          runners: ["agy", "gemini", "codex", "claude"],
          catalogueOnly: false,
          isolationGateRequired: false,
          isolationPassed: false,
          isolationMethod: "none",
          sha256Verified: true,
          enabled: isRunning || isFailure,
          autoDisabled: false,
          status: isRunning || isFailure ? "enabled" : "disabled",
          warning: "",
          tokensSavedTonight: isRunning ? 14200 : isFailure ? 9200 : 0,
          estimateLabel: isRunning
            ? "~14,200 tokens saved (estimate)"
            : isFailure
              ? "~9,200 tokens saved (estimate)"
              : "~0 tokens saved (estimate)",
          abAction: isRunning || isFailure ? "keep_enabled" : "keep_measuring",
          abReason: isRunning
            ? "repomix cut ~7,100 tokens per backend phase (estimate) with no extra retries."
            : "Waiting for baseline runs.",
        },
        {
          name: "agy-fast",
          displayName: "agy-fast",
          role: "Batched search, outline, edit, and check tools for Antigravity (auto-update off)",
          version: "fe635e1",
          artifact: "agy-fast-fe635e1.zip",
          sha256: "5f9b3d72e1a846c0b2d5f8e1a4c79036d2e8f1a5c9b3047e6a2d8f1c5b9e3a42",
          license: "MIT",
          runners: ["agy"],
          catalogueOnly: false,
          isolationGateRequired: true,
          isolationPassed: false,
          isolationMethod: "none",
          sha256Verified: true,
          enabled: false,
          autoDisabled: false,
          status: "isolation_blocked",
          warning:
            "agy-fast stayed disabled because Antigravity could not load it per run without modifying ~/.gemini/config.",
          tokensSavedTonight: 0,
          estimateLabel: "~0 tokens saved (estimate)",
          abAction: "keep_measuring",
          abReason: "Isolation probe required before enabling.",
        },
        {
          name: "rtk-oss",
          displayName: "rtk-oss",
          role: "Compresses shell output for Gemini CLI, Claude, and Codex (Apache-2.0 OSS build)",
          version: "1.2.1",
          artifact: "rtk-oss-x86_64-pc-windows-msvc.zip",
          sha256: "7d1a5e93c2b864f0e4a7c1d9b3f58206e1a9d4c7b2f8035e6c9a1d4f8b2e7c33",
          license: "Apache-2.0",
          runners: ["gemini", "claude", "codex"],
          catalogueOnly: false,
          isolationGateRequired: false,
          isolationPassed: false,
          isolationMethod: "none",
          sha256Verified: !isFailure,
          enabled: isRunning,
          autoDisabled: isFailure,
          status: isFailure ? "auto_disabled" : isRunning ? "enabled" : "disabled",
          warning: isFailure
            ? "rtk-oss was switched off automatically: retry turns rose from 0.3 to 1.0."
            : "",
          tokensSavedTonight: isRunning ? 4200 : 0,
          estimateLabel: isRunning ? "~4,200 tokens saved (estimate)" : "~0 tokens saved (estimate)",
          abAction: isFailure ? "auto_disable" : isRunning ? "keep_enabled" : "keep_measuring",
          abReason: isFailure
            ? "rtk-oss was switched off automatically: retry turns rose from 0.3 to 1.0."
            : "Bundled with portable rg.exe on runner PATH only.",
        },
        {
          name: "ripgrep",
          displayName: "ripgrep (rg.exe)",
          role: "Portable ripgrep binary prepended to runner PATH for rtk-oss filters",
          version: "14.1.1",
          artifact: "ripgrep-14.1.1-x86_64-pc-windows-msvc.zip",
          sha256: "9b3c7f15e4d286a0f6c9e3b1d5a70428f3c1e6d9b4a2057f8e1c3d6a9b4f2e54",
          license: "MIT/Unlicense",
          runners: ["gemini", "claude", "codex"],
          catalogueOnly: false,
          isolationGateRequired: false,
          isolationPassed: false,
          isolationMethod: "none",
          sha256Verified: true,
          enabled: isRunning,
          autoDisabled: false,
          status: isRunning ? "enabled" : "disabled",
          warning: "",
          tokensSavedTonight: 0,
          estimateLabel: "~0 tokens saved (estimate)",
          abAction: "keep_measuring",
          abReason: "Helper binary for rtk-oss.",
        },
        {
          name: "ast-grep-mcp",
          displayName: "ast-grep-mcp",
          role: "Structural code search (catalogue only; enable later if search is a top cost)",
          version: "0.1.0",
          artifact: "ast-grep-mcp-0.1.0.tgz",
          sha256: "1e5a9c37f2b408d6a8e1c5f3b7d92604a5e3c8f1d6b4097a2c5e8f1b4d7a3c65",
          license: "MIT",
          runners: ["agy", "gemini", "codex", "claude"],
          catalogueOnly: true,
          isolationGateRequired: false,
          isolationPassed: false,
          isolationMethod: "none",
          sha256Verified: false,
          enabled: false,
          autoDisabled: false,
          status: "catalogue_only",
          warning: "Catalogue entry only (disabled). Enable later if ccusage shows search is a top cost.",
          tokensSavedTonight: 0,
          estimateLabel: "~0 tokens saved (estimate)",
          abAction: "keep_measuring",
          abReason: "",
        },
      ],
      totalTokensSaved: isRunning ? 18400 : isFailure ? 9200 : 0,
      estimateLabel: isRunning
        ? "~18,400 tokens saved tonight (estimate)"
        : isFailure
          ? "~9,200 tokens saved tonight (estimate)"
          : "~0 tokens saved tonight (estimate)",
      warnings: isFailure
        ? [
            "ccusage did not report Antigravity usage; falling back to CLI JSON usage and run counts.",
            "rtk-oss was switched off automatically: retry turns rose from 0.3 to 1.0.",
          ]
        : [],
      toolsRoot: "C:\\Users\\AppData\\Local\\NightShift\\tools",
      telemetryOff: true,
      isEstimate: true,
    };
    if (scenario === "running") {
      this.phoneStatus = {
        enabled: true,
        url: "https://script.google.com/macros/s/demo-bridge/exec",
        hasSecret: true,
        lastResult: "ok 05:24",
        lastResultAt: "2026-11-26T05:24:00Z",
      };
    } else if (scenario === "failure") {
      this.phoneStatus = {
        enabled: true,
        url: "https://script.google.com/macros/s/demo-bridge/exec",
        hasSecret: true,
        lastResult: "failed: timeout",
        lastResultAt: "2026-11-26T05:24:00Z",
      };
    } else {
      this.phoneStatus = {
        enabled: false,
        url: "",
        hasSecret: false,
        lastResult: "not sent yet",
        lastResultAt: "",
      };
    }

    const isEmpty = scenario === "empty";
    const stageMaturity: PipelineState["stageMaturity"] = [
      { stage: "PLAN", role: "planner", maturity: "Integration-tested", description: "Frozen contracts and acceptance specs" },
      { stage: "CODE", role: "coder", maturity: "Integration-tested", description: "Risk-routed coding in isolated worktree" },
      { stage: "MECHANICAL", role: "mechanical", maturity: "Integration-tested", description: "Deterministic build, tests, screenshots, PR body" },
      { stage: "TRIAGE", role: "triage", maturity: "Integration-tested", description: "Compact <=10-line CI failure summary" },
      { stage: "FIX", role: "fixer", maturity: "Integration-tested", description: "Risk-routed targeted fix commits" },
      { stage: "REVIEW", role: "reviewer", maturity: "Integration-tested", description: "Cross-provider raw-diff review with validated citations" },
      { stage: "GATES", role: "gatekeeper", maturity: "Integration-tested", description: "Exact-SHA CI, real duration, artifacts, protected tests" },
      { stage: "MERGE", role: "releaser", maturity: "Integration-tested", description: "MergeGate chokepoint and outbox reconciliation" },
      { stage: "RELEASE", role: "releaser", maturity: "Integration-tested", description: "Post-merge CI, mandatory smoke test, one-click rollback" },
    ];

    if (isEmpty) {
      this.pipelineState = {
        summary: {
          acceptedChanges30d: 0,
          costPerAcceptedUsdEst: 0,
          regressionRate: 0,
          goldSavedCount: 0,
          freeOnlyMode: true,
          monthlySpendUsdEst: 0,
          monthlyCapUsdEst: 0,
          tonightSpendUsdEst: 0,
          tonightCapUsdEst: 0,
          isEstimate: true,
        },
        activeTask: null,
        tasks: [],
        modelScores: [],
        ledger: [],
        stageMaturity,
        releases: [],
      };
    } else {
      const govTask: PipelineState["tasks"][number] = {
        id: "p8b-t01",
        phase_id: "p8b",
        plan_id: "plan-1",
        title: isFailure ? "Governance PR #42: Protected test authority" : "Role-based pipeline evidence gate",
        branch: isFailure ? "feat/p8b-governance-tests" : "feat/p8b-evidence-gate",
        risk_class: isFailure ? "governance" : "business_logic",
        status: isFailure ? "BLOCKED" : isRunning ? "REVIEWING" : "RELEASED",
        blocked_from_status: isFailure ? "REVIEWING" : "",
        contract_id: "p8b-t01-v1",
        contract_sha256: "8f4c2a91b7d630e5f1a9c4e28b3d7061f5e9a2c8d4b7013f6e8a9c2d5b1f4e70",
        pr_number: 42,
        head_sha: "a1b2c3d4e5f60718293a",
        review_sha: "a1b2c3d4e5f60718293a",
        owner_approved_sha: null,
        block_reason: isFailure ? "awaiting_owner_approval" : "",
        block_detail: isFailure
          ? "Governance PR #42 changes test files and needs your approval before merge."
          : "",
        next_step: isFailure ? "Review & Approve" : "",
        contributingProviders: ["google"],
        reviewerProvider: "openai",
        evidence: [
          {
            id: 1,
            task_id: "p8b-t01",
            contract_sha256: "8f4c2a91b7d630e5f1a9c4e28b3d7061f5e9a2c8d4b7013f6e8a9c2d5b1f4e70",
            commit_sha: "a1b2c3d4e5f60718293a",
            kind: "ci_check",
            source: "circleci:desktop-backend",
            build_id: "cc-1042",
            duration_seconds: 34.2,
            artifact_paths_json: "[]",
            verified: 1,
            rejection_reason: "",
            created_at: "2026-11-27T04:12:00Z",
          },
          {
            id: 2,
            task_id: "p8b-t01",
            contract_sha256: "8f4c2a91b7d630e5f1a9c4e28b3d7061f5e9a2c8d4b7013f6e8a9c2d5b1f4e70",
            commit_sha: "a1b2c3d4e5f60718293a",
            kind: "screenshot",
            source: "playwright:1440x900",
            build_id: "cc-1043",
            duration_seconds: 52.8,
            artifact_paths_json: '["ui-screenshots/mission-control-running-1440x900.png"]',
            verified: 1,
            rejection_reason: "",
            created_at: "2026-11-27T04:13:00Z",
          },
        ],
        attempts: [
          {
            id: 1,
            stage: "CODE",
            role: "coder",
            engine_id: "agy",
            provider: "google",
            model: "neon",
            decision_reason: "Chose agy for business_logic at $0.00/accepted (estimate).",
            outcome: "ok",
            wall_seconds: 185,
            started_at: "2026-11-27T04:05:00Z",
          },
          {
            id: 2,
            stage: "REVIEW",
            role: "reviewer",
            engine_id: "codex",
            provider: "openai",
            model: "gpt-5",
            decision_reason: "Independent provider openai (excluded contributing: google).",
            outcome: isFailure ? "blocked" : "ok",
            wall_seconds: 48,
            started_at: "2026-11-27T04:14:00Z",
          },
        ],
      };

      const docsTask: PipelineState["tasks"][number] = {
        id: "p8b-t02",
        phase_id: "p8b",
        plan_id: "plan-1",
        title: "Update release notes documentation",
        branch: "feat/p8b-docs",
        risk_class: "docs_format",
        status: "RELEASED",
        blocked_from_status: "",
        contract_id: "p8b-t02-v1",
        contract_sha256: "2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a",
        pr_number: 41,
        head_sha: "f1e2d3c4b5a697887766",
        review_sha: "f1e2d3c4b5a697887766",
        owner_approved_sha: "f1e2d3c4b5a697887766",
        block_reason: "",
        block_detail: "",
        next_step: "",
        contributingProviders: ["alibaba"],
        reviewerProvider: "openai",
        evidence: [],
        attempts: [],
      };

      this.pipelineState = {
        summary: {
          acceptedChanges30d: 14,
          costPerAcceptedUsdEst: 0.02,
          regressionRate: 0.0,
          goldSavedCount: 6,
          freeOnlyMode: true,
          monthlySpendUsdEst: 0.0,
          monthlyCapUsdEst: 0.0,
          tonightSpendUsdEst: 0.0,
          tonightCapUsdEst: 0.0,
          isEstimate: true,
        },
        activeTask: govTask,
        tasks: [govTask, docsTask],
        modelScores: [
          {
            engine_id: "qwen",
            model: "qwen3-coder",
            role: "coder",
            risk_class: "docs_format",
            sample_tasks: 12,
            accepted_tasks: 11,
            total_attempts: 13,
            regression_count: 0,
            avg_retries: 0.08,
            avg_turnaround_min: 3.4,
            total_cost_usd_est: 0.09,
            cost_per_accepted_usd_est: 0.008,
            benchmark_graduated: 1,
            updated_at: "2026-11-27T04:00:00Z",
          },
          {
            engine_id: "deepseek",
            model: "deepseek-chat",
            role: "triage",
            risk_class: "business_logic",
            sample_tasks: 6,
            accepted_tasks: 5,
            total_attempts: 7,
            regression_count: 0,
            avg_retries: 0.17,
            avg_turnaround_min: 1.2,
            total_cost_usd_est: 0.06,
            cost_per_accepted_usd_est: 0.012,
            benchmark_graduated: 0,
            updated_at: "2026-11-27T04:00:00Z",
          },
          {
            engine_id: "codex",
            model: "gpt-5",
            role: "reviewer",
            risk_class: "business_logic",
            sample_tasks: 11,
            accepted_tasks: 10,
            total_attempts: 11,
            regression_count: 0,
            avg_retries: 0.1,
            avg_turnaround_min: 2.1,
            total_cost_usd_est: 0.44,
            cost_per_accepted_usd_est: 0.044,
            benchmark_graduated: 1,
            updated_at: "2026-11-27T04:00:00Z",
          },
        ],
        ledger: [
          {
            id: 1,
            task_id: "p8b-t01",
            role: "coder",
            engine_id: "agy",
            provider: "google",
            model: "neon",
            risk_class: isFailure ? "governance" : "business_logic",
            tokens_in: 18400,
            tokens_out: 4100,
            est_token_cost_usd: 0.0,
            ci_minutes: 3.2,
            artifact_mb: 14.5,
            wall_seconds: 185,
            outcome: "accepted",
            is_estimate: 1,
            created_at: "2026-11-27T04:05:00Z",
          },
        ],
        stageMaturity,
        releases: [
          {
            tag: "v1.0.0-preview.45",
            task_id: "p8b-t01",
            repo: "owner/nightshift-desktop",
            merge_sha: "9988776655443322",
            previous_good_tag: "v1.0.0-preview.44",
            artifact_sha256: "abc123def456",
            smoke_command: "NightShift.exe --self-test",
            smoke_status: isFailure ? "failed" : "passed",
            smoke_detail: isFailure
              ? "Post-release self-test exited with code 1. Previous good release v1.0.0-preview.44 is ready for one-click rollback."
              : "NightShift.exe --self-test exited 0 and reported OK.",
            verified_at: isFailure ? null : "2026-11-27T04:20:00Z",
            rolled_back_at: null,
          },
        ],
      };
    }
  }

  async call<T = unknown>(method: string, args?: Record<string, unknown>): Promise<T> {
    switch (method) {
      case "app.snapshot":
        return structuredClone({
          ...this.snapshot,
          stopArmed: this.stopArmed,
        }) as T;

      case "app.info":
        return {
          version: "0.1.0",
          apiVersion: "1",
          scenario: this.scenario,
          mode: "mock",
          modules: ["mission-control", "plan-builder", "runs-history", "engines-quota", "settings"],
        } as T;

      case "app.firstRunStatus": {
        const hasProjects = this.snapshot.projects.length > 0;
        return {
          isFirstRun: this.snapshot.scenario === "empty" && !this.firstRunDismissed && !hasProjects,
          dismissed: this.firstRunDismissed,
          hasProjects,
        } as T;
      }

      case "app.dismissFirstRun":
        this.firstRunDismissed = true;
        return { dismissed: true } as T;

      case "app.selectProject": {
        const projectId = typeof args?.projectId === "string" ? args.projectId : null;
        this.snapshot = { ...this.snapshot, activeProjectId: projectId };
        this.emit("app.snapshot", structuredClone(this.snapshot));
        return structuredClone(this.snapshot) as T;
      }

      case "logins.status":
      case "logins.recheck": {
        const logins = this.snapshot.logins ?? structuredClone(SCENARIO_FIXTURES.idle.logins);
        return logins as T;
      }

      case "logins.openFix": {
        const tool = String(args?.tool ?? "");
        // If fixing a logged-out tool in mock, simulate tool becoming ready
        if (this.snapshot.logins && this.snapshot.logins[tool]) {
          const current = this.snapshot.logins[tool] as LoginCheckItem;
          this.snapshot.logins[tool] = {
            ...current,
            state: "Ready",
            detail: `${tool} login verified`,
          };
          this.emit("app.snapshot", structuredClone(this.snapshot));
        }
        return { ok: true, opened: true } as T;
      }

      case "engine.status":
        return {
          running: this.snapshot.runState === "running",
          runState: this.snapshot.runState,
          stopArmed: this.stopArmed,
          deck: this.snapshot.deck,
        } as T;

      case "engine.start":
        this.snapshot = {
          ...this.snapshot,
          runState: "running",
          headline: "All good - Night-Shift overnight run started.",
        };
        this.emit("app.snapshot", structuredClone(this.snapshot));
        this.emit("nightshift:event", {
          kind: "run.started",
          plain_text: "Night-Shift overnight run started.",
        });
        return { ok: true, started: true } as T;

      case "engine.stopAfterPhase":
        this.stopArmed = true;
        this.snapshot = { ...this.snapshot, stopArmed: true };
        this.emit("app.snapshot", structuredClone(this.snapshot));
        return {
          armed: true,
          message: "Night-Shift will stop after the current phase finishes.",
        } as T;

      case "engine.undoStop":
        this.stopArmed = false;
        this.snapshot = { ...this.snapshot, stopArmed: false };
        this.emit("app.snapshot", structuredClone(this.snapshot));
        return {
          armed: false,
          message: "Stop after current phase cancelled.",
        } as T;

      case "plan.get": {
        const isEmpty = this.snapshot.scenario === "empty";
        const mockPhases = isEmpty
          ? []
          : [
              {
                id: "p1",
                title: "Foundation & Schema Migrations",
                branch: "feat/p1-schema",
                prompt: "Add SQLite schema migrations for engine audit events and settings.",
                depends_on: [],
                ladder: "backend",
                timeout_minutes: 60,
                auto_merge: true,
                release_after_merge: false,
                implementer: "Antigravity",
              },
              {
                id: "p2",
                title: "Engine Core State Machine",
                branch: "feat/p2-engine",
                prompt: "Implement failover state machine, single-instance guard, and process cleanup.",
                depends_on: ["p1"],
                ladder: "backend",
                timeout_minutes: 90,
                auto_merge: true,
                release_after_merge: false,
                implementer: "Antigravity",
              },
              {
                id: "p3",
                title: "Design Tokens & Primitives",
                branch: "feat/p3-design",
                prompt: "Generate Tailwind tokens theme, GlassCard, StatusPill, and EngineCard components.",
                depends_on: ["p1"],
                ladder: "ui",
                timeout_minutes: 45,
                auto_merge: true,
                release_after_merge: false,
                implementer: "Gemini CLI",
                overrideImplementer: "Gemini CLI",
              },
              {
                id: "p4",
                title: "Mission Control Live Deck",
                branch: "feat/p4-mission-control",
                prompt: "Connect typed bridge, live hero ring, tool logins row, and toast events.",
                depends_on: ["p2", "p3"],
                ladder: "ui",
                timeout_minutes: 120,
                auto_merge: true,
                release_after_merge: false,
                implementer: "Codex CLI",
              },
              {
                id: "p5",
                title: "Release Verification & Artifact Packaging",
                branch: "feat/p5-release",
                prompt: "Add artifact glob matcher, SHA256SUMS generator, and GitHub preview release tagger.",
                depends_on: ["p4"],
                ladder: "backend",
                timeout_minutes: 180,
                auto_merge: true,
                release_after_merge: true,
                implementer: "Claude Code",
              },
            ];

        return {
          plan: { id: "plan_mock", title: "Continuous Delivery Plan", project_id: this.snapshot.activeProjectId },
          phases: mockPhases,
        } as T;
      }

      case "plan.validate": {
        const isFailure = this.snapshot.scenario === "failure";
        if (isFailure) {
          return {
            valid: false,
            problems: [
              "Phase 'p4' depends on non-existent phase 'p99'.",
              "Phase 'p5' needs a branch.",
            ],
          } as T;
        }
        return { valid: true, problems: [] } as T;
      }

      case "council.run":
      case "council.session": {
        const goal = String(args?.goal ?? "Add offline status caching and auto-sync to the mobile app");
        const isSub = this.scenario === "council-substitution";
        const isDrafting = this.scenario === "council-drafting";
        const isCritique = this.scenario === "council-critique";

        const draftPhases = [
          { id: "p1", title: "Cache schema & SQLite store", prompt: "Build offline storage table and migration", branch: "feat/p1-cache", depends_on: [], size: "medium", timeout_minutes: 90 },
          { id: "p2", title: "Background sync worker", prompt: "Implement network listener and sync loop", branch: "feat/p2-sync", depends_on: ["p1"], size: "medium", timeout_minutes: 120 },
        ];

        const critiquePhases = [
          { id: "p1", title: "Cache schema & SQLite store", prompt: "Build offline storage table and migration with retry guard", branch: "feat/p1-cache", depends_on: [], size: "medium", timeout_minutes: 90 },
          { id: "p2", title: "Network connectivity monitor", prompt: "Detect network restoration before triggering sync worker", branch: "feat/p2-monitor", depends_on: ["p1"], size: "small", timeout_minutes: 60 },
          { id: "p3", title: "Background sync worker", prompt: "Execute sync loop with backoff exponential guards", branch: "feat/p3-sync", depends_on: ["p2"], size: "medium", timeout_minutes: 120 },
        ];

        const finalPhases = [
          { id: "p1", title: "Cache schema & SQLite store", prompt: "Build offline storage table and migration with retry guard", branch: "feat/p1-cache", depends_on: [], size: "medium", timeout_minutes: 90 },
          { id: "p2", title: "Network connectivity monitor", prompt: "Detect network restoration before triggering sync worker", branch: "feat/p2-monitor", depends_on: ["p1"], size: "small", timeout_minutes: 60 },
          { id: "p3", title: "Background sync worker & conflict resolution", prompt: "Execute sync loop with backoff exponential guards and server timestamp wins resolution", branch: "feat/p3-sync", depends_on: ["p2"], size: "medium", timeout_minutes: 120 },
        ];

        const session = {
          goal,
          planId: "council-plan-101",
          diffSummary: isDrafting
            ? "Gemini is currently drafting initial phases from your goal and repo structure."
            : isCritique
              ? "Draft completed 2 phases. Codex is critiquing missing network monitor boundary."
              : "Draft created 2 phases. Critique added connectivity monitor (3 phases). Final review confirmed conflict resolution policy.",
          tradeoffSummary: "Separated network monitoring from sync worker to prevent unbounded retries during intermittent connectivity. Preserves gold quota.",
          roles: {
            draft: {
              role: "draft",
              engineId: isSub ? "codex" : "gemini",
              name: isSub ? "Codex CLI" : "Gemini CLI",
              model: isSub ? "o3-mini" : "gemini-2.5-pro",
              state: isDrafting ? "active" : "done",
              substituted: isSub,
              substitutionReason: isSub ? "Gemini cooling down until 03:45; Codex substituted" : undefined,
              elapsedSeconds: isDrafting ? 4.2 : 14.5,
              tokensEstimate: 14200,
              costUsdEstimate: 0.02,
              notes: ["Identified core caching and worker requirements from codebase."],
              phases: draftPhases,
            },
            critique: {
              role: "critique",
              engineId: "codex",
              name: "Codex CLI",
              model: "o3-mini",
              state: isDrafting ? "waiting" : isCritique ? "active" : "done",
              substituted: false,
              elapsedSeconds: isDrafting ? 0 : isCritique ? 3.8 : 11.2,
              tokensEstimate: isDrafting ? 0 : 18500,
              costUsdEstimate: isDrafting ? 0 : 0.05,
              notes: ["Phase 2 was too large and lacked network state verification."],
              issues: ["Missing connectivity monitor step", "Unbounded retry loop risk on network drop"],
              phases: isDrafting ? [] : critiquePhases,
            },
            final: {
              role: "final",
              engineId: "claude",
              name: "Claude Code",
              model: "claude-3-7-sonnet",
              state: isDrafting || isCritique ? "waiting" : "done",
              substituted: false,
              elapsedSeconds: isDrafting || isCritique ? 0 : 6.1,
              tokensEstimate: isDrafting || isCritique ? 0 : 8400,
              costUsdEstimate: isDrafting || isCritique ? 0 : 0.09,
              notes: ["Resolved dispute by adding server timestamp conflict resolution."],
              tradeoffSummary: "Separated network monitoring from sync worker to prevent unbounded retries during intermittent connectivity. Preserves gold quota.",
              phases: isDrafting || isCritique ? [] : finalPhases,
            },
          },
        };

        return session as T;
      }

      case "council.approve":
        return { ok: true, planId: "council-plan-101" } as T;

      case "engines.test":
        return { ok: true, output: "CLI test OK (v2.5.0)" } as T;

      case "engines.saveKey":
      case "engines.addEngine":
        return { ok: true } as T;

      case "plan.save":
      case "plan.setImplementer":
        return { ok: true, saved: true } as T;

      case "plan.importQueue": {
        const importedProject = {
          id: "owner/imported-repo",
          name: "owner/imported-repo",
          repo: "owner/imported-repo",
        };
        const existingProjects = this.snapshot.projects.some((p) => p.id === importedProject.id)
          ? this.snapshot.projects
          : [...this.snapshot.projects, importedProject];
        this.firstRunDismissed = true;
        this.snapshot = {
          ...this.snapshot,
          scenario: this.snapshot.scenario === "empty" ? "idle" : this.snapshot.scenario,
          headline:
            this.snapshot.scenario === "empty"
              ? "Ready - Imported legacy queue.json for owner/imported-repo."
              : this.snapshot.headline,
          activeProjectId: importedProject.id,
          projects: existingProjects,
        };
        this.emit("app.snapshot", structuredClone(this.snapshot));
        return {
          project: { ...importedProject, artifact_glob: "*.apk" },
          plan: { id: `plan_${importedProject.id}`, title: "Imported Queue Plan", project_id: importedProject.id },
          phases: [
            { id: "p1", title: "Imported Phase 1", branch: "feat/p1", depends_on: [], ladder: "backend" },
          ],
        } as T;
      }

      case "plan.exportQueue":
        return {
          repo: "owner/nightshift-desktop",
          local_repo: "D:/DevCache/nightshift-desktop",
          desktop_apk_dir: "D:/DevCache/release",
          artifact_glob: "*.apk",
          auto_merge: true,
          auto_release: false,
          max_ci_retries: 3,
          quota_cooldown_minutes: 60,
          runner_timeout_minutes: 90,
          failover_ladders: {
            backend: ["agy", "gemini", "codex", "claude"],
            ui: ["agy", "gemini", "claude"],
          },
          phases: [
            { id: "p1", title: "Foundation", branch: "feat/p1", prompt: "Set up base", depends_on: [], ladder: "backend" },
          ],
        } as T;

      case "runs.list": {
        const isEmpty = this.snapshot.scenario === "empty";
        if (isEmpty) return [] as T;

        return [
          {
            id: "run_20261125_02",
            title: "Nightly Continuous Delivery #14",
            projectId: this.snapshot.activeProjectId ?? "nightshift-desktop",
            status: this.snapshot.runState === "running" ? "running" : (this.snapshot.scenario === "failure" ? "failed" : "completed"),
            phasesCount: 5,
            phasesDone: this.snapshot.runState === "running" ? 3 : (this.snapshot.scenario === "failure" ? 3 : 5),
            phasesFailed: this.snapshot.scenario === "failure" ? 1 : 0,
            duration: "1 h 42 m",
            aiUsed: ["agy", "gemini", "codex", "claude"],
            createdAt: "2026-11-25T22:15:00Z",
            updatedAt: "2026-11-25T23:57:00Z",
          },
          {
            id: "run_20261124_01",
            title: "Nightly Continuous Delivery #13",
            projectId: this.snapshot.activeProjectId ?? "nightshift-desktop",
            status: "completed",
            phasesCount: 4,
            phasesDone: 4,
            phasesFailed: 0,
            duration: "1 h 18 m",
            aiUsed: ["agy", "gemini"],
            createdAt: "2026-11-24T22:10:00Z",
            updatedAt: "2026-11-24T23:28:00Z",
          },
          {
            id: "run_20261123_01",
            title: "Nightly Continuous Delivery #12",
            projectId: this.snapshot.activeProjectId ?? "nightshift-desktop",
            status: "completed",
            phasesCount: 3,
            phasesDone: 3,
            phasesFailed: 0,
            duration: "54 m",
            aiUsed: ["agy", "codex"],
            createdAt: "2026-11-23T22:30:00Z",
            updatedAt: "2026-11-23T23:24:00Z",
          },
        ] as T;
      }

      case "runs.get": {
        const runId = String(args?.runId ?? "run_20261125_02");
        return {
          run: {
            id: runId,
            title: "Nightly Continuous Delivery #14",
            projectId: this.snapshot.activeProjectId ?? "nightshift-desktop",
            status: "completed",
            phasesCount: 5,
            phasesDone: 5,
            phasesFailed: 0,
            duration: "1 h 42 m",
            aiUsed: ["agy", "gemini", "codex", "claude"],
            createdAt: "2026-11-25T22:15:00Z",
            updatedAt: "2026-11-25T23:57:00Z",
          },
          digest: [
            "# Morning Digest for Nightly Continuous Delivery #14",
            "",
            "**Status**: All 5 planned phases merged cleanly.",
            "**Total Duration**: 1 h 42 m across 4 engine handoffs.",
            "",
            "### Completed Phases",
            "- **Phase 1: Foundation & Schema Migrations**: Merged via Antigravity (PR #10)",
            "- **Phase 2: Engine Core State Machine**: Merged via Antigravity (PR #11)",
            "- **Phase 3: Design Tokens & Primitives**: Merged via Gemini CLI (PR #12)",
            "- **Phase 4: Mission Control Live Deck**: Merged via Codex CLI (PR #13)",
            "- **Phase 5: Release Verification & Packaging**: Merged via Claude Code (PR #14)",
            "",
            "### AI Usage Summary",
            "- **Antigravity**: 42,000 tokens (Primary implementer)",
            "- **Gemini CLI**: 18,500 tokens (UI & Review)",
            "- **Codex CLI**: 12,000 tokens (CI self-healing)",
            "- **Claude Code**: 4,500 tokens (Complex fallback)",
          ].join("\n"),
          phases: [
            { id: "p1", title: "Foundation & Schema Migrations", status: "completed", runner: "agy", pr_number: 10, pr_url: "https://github.com/owner/repo/pull/10", duration: "18 m" },
            { id: "p2", title: "Engine Core State Machine", status: "completed", runner: "agy", pr_number: 11, pr_url: "https://github.com/owner/repo/pull/11", duration: "25 m" },
            { id: "p3", title: "Design Tokens & Primitives", status: "completed", runner: "gemini", pr_number: 12, pr_url: "https://github.com/owner/repo/pull/12", duration: "20 m" },
            { id: "p4", title: "Mission Control Live Deck", status: "completed", runner: "codex", pr_number: 13, pr_url: "https://github.com/owner/repo/pull/13", duration: "22 m" },
            { id: "p5", title: "Release Verification & Packaging", status: "completed", runner: "claude", pr_number: 14, pr_url: "https://github.com/owner/repo/pull/14", duration: "17 m" },
          ],
          artifacts: [
            { name: "nightshift-desktop-v0.1.0-preview.exe", size: "32.4 MB", sha256: "7f4c9a81e32d..." },
            { name: "SHA256SUMS", size: "140 B", sha256: "e4d3a2b109af..." },
          ],
        } as T;
      }

      case "runs.openArtifactFolder":
        return { opened: true, path: "D:\\DevCache\\release" } as T;

      case "engines.list": {
        const isClaudeLimit = this.snapshot.scenario === "claude-limit";
        const isGeminiLoggedOut = this.snapshot.scenario === "gemini-logged-out";
        return [
          {
            id: "agy",
            runner: "agy",
            name: "Antigravity",
            model: "gemini-4-neon",
            effort: "medium",
            costTier: "standard",
            state: "ready",
            coolingUntil: null,
            quotaUsedPct: 65,
            skills: { ui: 5, backend: 4, fix_ci: 4, plan: 3 },
          },
          {
            id: "gemini",
            runner: "gemini",
            name: "Gemini CLI",
            model: "gemini-2.5-pro",
            effort: "medium",
            costTier: "standard",
            state: isGeminiLoggedOut ? "no-login" : "ready",
            coolingUntil: null,
            quotaUsedPct: 35,
            skills: { plan: 5, docs: 4, review: 4, backend: 3 },
          },
          {
            id: "codex",
            runner: "codex",
            name: "Codex CLI",
            model: "o3-mini",
            effort: "medium",
            costTier: "standard",
            state: "ready",
            coolingUntil: null,
            quotaUsedPct: 20,
            skills: { review: 5, plan: 4, fix_ci: 4, backend: 4 },
          },
          {
            id: "claude",
            runner: "claude",
            name: "Claude Code",
            model: "claude-3-7-sonnet",
            effort: "medium",
            costTier: "gold",
            state: isClaudeLimit ? "cooling" : "ready",
            coolingUntil: isClaudeLimit ? "04:30" : null,
            quotaUsedPct: 92,
            skills: { review: 5, plan: 5, backend: 5, ui: 4 },
          },
          {
            id: "qwen",
            runner: "qwen",
            name: "Qwen Code",
            model: "qwen-2.5-coder-32b",
            effort: "medium",
            costTier: "economy",
            state: "ready",
            coolingUntil: null,
            quotaUsedPct: 12,
            skills: { backend: 4, fix_ci: 3, ui: 3, docs: 3 },
          },
        ] as T;
      }

      case "engines.update":
        return { ok: true } as T;

      case "engines.getLadders":
        return {
          ladders: {
            backend: ["agy", "gemini", "codex", "claude"],
            ui: ["agy", "gemini", "claude"],
            review: ["gemini", "codex", "claude"],
            ci_fix: ["codex", "gemini", "claude"],
          },
          pinnedNote: "Claude is pinned last due to token cost; reserved for complex failovers.",
        } as T;

      case "engines.setLadders":
        return { ok: true, ladders: args?.ladders } as T;

      case "engines.getLedger":
        return {
          entries: [],
          routerDecisions: [
            { id: 1, step: "Phase 4 - Implementation", timestamp: "01:42", chosen: "Codex CLI", reason: "Claude cooling down until 04:30; Antigravity on phase 2." },
            { id: 2, step: "Phase 3 - Review Gate", timestamp: "01:15", chosen: "Gemini CLI", reason: "Highest review score (94) and standard cost tier." },
            { id: 3, step: "Phase 2 - Implementation", timestamp: "00:40", chosen: "Antigravity", reason: "Primary economy engine for backend modules." },
          ],
          chartData: [
            { runner: "agy", name: "Antigravity", tokens: 42000, costEstimate: "$0.00 (Flat)", pct: 65 },
            { runner: "gemini", name: "Gemini CLI", tokens: 18500, costEstimate: "$0.04 (Est.)", pct: 30 },
            { runner: "codex", name: "Codex CLI", tokens: 12000, costEstimate: "$0.12 (Est.)", pct: 20 },
            { runner: "claude", name: "Claude Code", tokens: 4500, costEstimate: "$0.09 (Est.)", pct: 10 },
          ],
          isEstimate: true,
        } as T;

      case "settings.getProjects": {
        const isEmpty = this.snapshot.scenario === "empty";
        if (isEmpty) return [] as T;
        return [
          {
            id: "nightshift-desktop",
            name: "Night-Shift Desktop",
            repo: "owner/nightshift-desktop",
            local_path: "D:\\DevCache\\nightshift-desktop",
            build_rules: "npm run lint && npm test",
            artifact_glob: "*.exe",
            output_dir: "D:\\DevCache\\release",
          },
          {
            id: "drivelift-android",
            name: "DriveLift Android",
            repo: "owner/drivelift-android",
            local_path: "D:\\DevCache\\drivelift-android",
            build_rules: "./gradlew assembleDebug",
            artifact_glob: "*.apk",
            output_dir: "D:\\DevCache\\drivelift\\release",
          },
        ] as T;
      }

      case "settings.saveProject":
      case "settings.deleteProject":
        return { ok: true } as T;

      case "settings.getPriorityList":
        return [
          { position: 1, engine: "qwen", model: "qwen-2.5-coder-32b", effort: "medium", availability: "Ready", backAt: null },
          { position: 2, engine: "agy", model: "gemini-4-neon", effort: "medium", availability: "Ready", backAt: null },
          {
            position: 3,
            engine: "gemini",
            model: "gemini-2.5-pro",
            effort: "medium",
            availability: this.snapshot.scenario === "gemini-logged-out" ? "Logged out" : "Ready",
            backAt: null,
          },
          { position: 4, engine: "codex", model: "o3-mini", effort: "medium", availability: "Ready", backAt: null },
          {
            position: 5,
            engine: "claude",
            model: "claude-3-7-sonnet",
            effort: "medium",
            availability: this.snapshot.scenario === "claude-limit" ? "Cooling down" : "Ready",
            backAt: this.snapshot.scenario === "claude-limit" ? "03:10" : null,
          },
        ] as T;

      case "settings.setPriorityList":
        return { ok: true } as T;

      case "settings.getGuardLimits":
        return {
          maxAttempts: 3,
          maxCiRetries: 4,
          maxRunInvocations: 30,
          runnerTimeoutMinutes: 90,
        } as T;

      case "settings.setGuardLimits":
        return { ok: true } as T;

      case "settings.getDataFolder":
        return { path: "C:\\Users\\AppData\\Local\\NightShift" } as T;

      case "settings.openDataFolder":
        return { opened: true, path: "C:\\Users\\AppData\\Local\\NightShift" } as T;

      case "settings.setKeepAwake":
        return { ok: true, keepAwake: Boolean(args?.enabled) } as T;

      case "settings.getAutostart":
        return {
          enabled: this.autostartEnabled,
          supported: true,
          method: "task-scheduler",
        } as T;

      case "settings.setAutostart": {
        this.autostartEnabled = Boolean(args?.enabled);
        return {
          enabled: this.autostartEnabled,
          supported: true,
          method: "task-scheduler",
          detail: this.autostartEnabled
            ? "Night-Shift will start on Windows sign-in (per-user task)."
            : "Autostart turned off.",
        } as T;
      }

      case "settings.get":
        return { key: args?.key, value: "default" } as T;

      case "settings.set":
        return { ok: true, key: args?.key, value: args?.value } as T;

      case "phone_status.getSettings":
        return structuredClone(this.phoneStatus) as T;

      case "phone_status.saveSettings": {
        if (typeof args?.enabled === "boolean") {
          this.phoneStatus.enabled = args.enabled;
        }
        if (typeof args?.url === "string") {
          this.phoneStatus.url = args.url.trim();
        }
        if (args?.clearSecret) {
          this.phoneStatus.hasSecret = false;
        } else if (typeof args?.secret === "string" && args.secret.trim()) {
          this.phoneStatus.hasSecret = true;
        }
        return structuredClone(this.phoneStatus) as T;
      }

      case "phone_status.sendTest": {
        if (!this.phoneStatus.url || !this.phoneStatus.hasSecret) {
          this.phoneStatus.lastResult = "failed: missing URL or secret";
          this.phoneStatus.lastResultAt = "2026-11-26T05:25:00Z";
          return {
            ok: false,
            lastResult: this.phoneStatus.lastResult,
            lastResultAt: this.phoneStatus.lastResultAt,
          } as T;
        }
        this.phoneStatus.lastResult = "ok 05:25";
        this.phoneStatus.lastResultAt = "2026-11-26T05:25:00Z";
        return {
          ok: true,
          lastResult: this.phoneStatus.lastResult,
          lastResultAt: this.phoneStatus.lastResultAt,
        } as T;
      }

      case "token_optimizer.getState":
        return structuredClone(this.tokenOptimizerState) as T;

      case "token_optimizer.setToolEnabled": {
        const toolName = String(args?.tool ?? "");
        const nextEnabled = Boolean(args?.enabled);
        this.tokenOptimizerState.tools = this.tokenOptimizerState.tools.map((t) => {
          if (t.name !== toolName) return t;
          if (t.catalogueOnly || (t.isolationGateRequired && !t.isolationPassed && nextEnabled)) {
            return t;
          }
          return {
            ...t,
            enabled: nextEnabled,
            autoDisabled: nextEnabled ? false : t.autoDisabled,
            status: nextEnabled ? "enabled" : "disabled",
          };
        });
        return structuredClone(this.tokenOptimizerState) as T;
      }

      case "token_optimizer.verifyTools":
        return structuredClone(this.tokenOptimizerState) as T;

      case "token_optimizer.runIsolationProbe": {
        const probeResult = {
          passed: false,
          method: "none",
          reason:
            "agy-fast stayed disabled because Antigravity could not load it per run via --settings or a project-local folder without global config edits.",
          globalConfigModified: false,
          candidatesTried: ["settings_flag", "project_local"],
        };
        this.tokenOptimizerState = {
          ...this.tokenOptimizerState,
          probeResult,
        };
        return structuredClone(this.tokenOptimizerState) as T;
      }

      case "token_optimizer.evaluateAb":
        return structuredClone(this.tokenOptimizerState) as T;

      case "pipeline.getState":
        return structuredClone(this.pipelineState) as T;

      case "pipeline.approveTask": {
        const taskId = String(args?.taskId ?? "");
        this.pipelineState.tasks = this.pipelineState.tasks.map((t) =>
          t.id === taskId
            ? {
                ...t,
                status: "APPROVED",
                owner_approved_sha: t.head_sha ?? "approved",
                block_reason: "",
                block_detail: "",
                next_step: "",
              }
            : t,
        );
        if (this.pipelineState.activeTask?.id === taskId) {
          this.pipelineState.activeTask =
            this.pipelineState.tasks.find((t) => t.id === taskId) ?? this.pipelineState.activeTask;
        }
        return structuredClone(this.pipelineState.activeTask) as T;
      }

      case "pipeline.rollbackRelease": {
        const tag = String(args?.tag ?? "");
        this.pipelineState.releases = this.pipelineState.releases.map((r) =>
          r.tag === tag
            ? {
                ...r,
                smoke_status: "rolled_back",
                smoke_detail: `Superseded and rolled back to ${r.previous_good_tag ?? "previous release"}.`,
                rolled_back_at: "2026-11-27T04:30:00Z",
              }
            : r,
        );
        return { ok: true, faultyTag: tag } as T;
      }

      case "window.minimize":
      case "window.toggleMaximize":
      case "window.close":
        return { ok: true } as T;

      default:
        return { ok: true, method, args: args ?? {} } as T;
    }
  }

  subscribe(event: string, cb: BridgeEventCallback): () => void {
    let bucket = this.listeners.get(event);
    if (!bucket) {
      bucket = new Set();
      this.listeners.set(event, bucket);
    }
    bucket.add(cb);
    return () => {
      bucket?.delete(cb);
    };
  }

  emit(event: string, payload: unknown): void {
    const bucket = this.listeners.get(event);
    if (bucket) {
      for (const cb of bucket) {
        cb(payload);
      }
    }
    const wildcard = this.listeners.get("*");
    if (wildcard) {
      for (const cb of wildcard) {
        cb(payload);
      }
    }
  }

  /**
   * Replay a sequence of realistic engine events on the event bus.
   */
  playStream(streamKey: keyof typeof REALISTIC_STREAMS): void {
    const stream = REALISTIC_STREAMS[streamKey];
    if (!stream) return;
    for (const evt of stream) {
      this.emit("nightshift:event", evt);
      this.emit(evt.kind, evt);
    }
  }
}
