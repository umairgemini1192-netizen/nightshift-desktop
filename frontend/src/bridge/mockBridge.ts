import { SCENARIO_FIXTURES } from "./fixtures";
import type {
  AppSnapshot,
  Bridge,
  BridgeEventCallback,
  LoginCheckItem,
  ScenarioName,
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

  constructor(scenario: ScenarioName = "idle") {
    this.scenario = scenario;
    this.snapshot = structuredClone(SCENARIO_FIXTURES[scenario] ?? SCENARIO_FIXTURES.idle);
    this.stopArmed = Boolean(this.snapshot.stopArmed);
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

      case "plan.save":
      case "plan.setImplementer":
        return { ok: true, saved: true } as T;

      case "plan.importQueue":
        return {
          ok: true,
          data: {
            project: { id: "imported", repo: "owner/imported-repo" },
            plan: { id: "plan_imported", title: "Imported Queue Plan" },
            phases: [
              { id: "p1", title: "Imported Phase 1", branch: "feat/p1", depends_on: [], ladder: "backend" },
            ],
          },
        } as T;

      case "plan.exportQueue":
        return {
          repo: "owner/nightshift-desktop",
          local_repo: "D:/DevCache/nightshift-desktop",
          desktop_apk_dir: "D:/DevCache/release",
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
            model: "auto",
            effort: "medium",
            costTier: "economy",
            state: "ready",
            coolingUntil: null,
            quotaUsedPct: 65,
            skills: { backend: 95, ui: 90, review: 85, test: 92 },
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
            skills: { backend: 90, ui: 88, review: 94, test: 88 },
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
            skills: { backend: 92, ui: 85, review: 90, test: 95 },
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
            skills: { backend: 96, ui: 94, review: 96, test: 94 },
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
          { position: 1, engine: "agy", model: "auto", effort: "medium" },
          { position: 2, engine: "gemini", model: "gemini-2.5-pro", effort: "medium" },
          { position: 3, engine: "codex", model: "o3-mini", effort: "medium" },
          { position: 4, engine: "claude", model: "claude-3-7-sonnet", effort: "medium" },
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

      case "settings.openDataFolder":
        return { opened: true, path: "C:\\Users\\AppData\\Local\\NightShift" } as T;

      case "settings.setKeepAwake":
        return { ok: true, keepAwake: Boolean(args?.enabled) } as T;

      case "settings.get":
        return { key: args?.key, value: "default" } as T;

      case "settings.set":
        return { ok: true, key: args?.key, value: args?.value } as T;

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
