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

      case "plan.get":
        return {
          plan: { id: "plan_mock", title: "Active Plan", project_id: this.snapshot.activeProjectId },
          phases: (this.snapshot.deck?.done_tonight ?? []).concat(this.snapshot.deck?.next ?? []),
        } as T;

      case "runs.list":
        return [
          {
            id: "run_mock_1",
            title: "Nightly Build Run",
            phasesCount: 5,
            phasesDone: this.snapshot.deck?.done_tonight?.length ?? 0,
            status: this.snapshot.runState,
          },
        ] as T;

      case "engines.list":
        return [
          { id: "agy", runner: "agy", model: "auto", effort: "medium", state: "ready" },
          { id: "gemini", runner: "gemini", model: "gemini-2.5-pro", effort: "medium", state: "ready" },
          { id: "codex", runner: "codex", model: "o3-mini", effort: "medium", state: "ready" },
          { id: "claude", runner: "claude", model: "claude-3-7-sonnet", effort: "medium", state: "ready" },
        ] as T;

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
