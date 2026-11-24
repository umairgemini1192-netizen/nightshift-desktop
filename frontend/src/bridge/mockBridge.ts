import { SCENARIO_FIXTURES } from "./fixtures";
import type { AppSnapshot, Bridge, BridgeEventCallback, ScenarioName } from "./types";

export class MockBridge implements Bridge {
  private scenario: ScenarioName;
  private snapshot: AppSnapshot;
  private listeners = new Map<string, Set<BridgeEventCallback>>();

  constructor(scenario: ScenarioName = "idle") {
    this.scenario = scenario;
    this.snapshot = structuredClone(SCENARIO_FIXTURES[scenario]);
  }

  async call<T = unknown>(method: string, args?: Record<string, unknown>): Promise<T> {
    switch (method) {
      case "app.snapshot":
        return structuredClone(this.snapshot) as T;
      case "app.info":
        return {
          version: "0.1.0",
          apiVersion: "1",
          scenario: this.scenario,
          mode: "mock",
        } as T;
      case "app.selectProject": {
        const projectId = typeof args?.projectId === "string" ? args.projectId : null;
        this.snapshot = { ...this.snapshot, activeProjectId: projectId };
        this.emit("app.snapshot", structuredClone(this.snapshot));
        return structuredClone(this.snapshot) as T;
      }
      case "window.minimize":
      case "window.toggleMaximize":
      case "window.close":
        return { ok: true } as T;
      case "engine.stopAfterPhase":
        return { armed: true, message: "Night-Shift will stop after the current phase finishes." } as T;
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
    if (!bucket) return;
    for (const cb of bucket) {
      cb(payload);
    }
  }
}
