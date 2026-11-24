import { describe, expect, it, vi } from "vitest";
import { createBridge, MockBridge, parseScenarioParam, type AppSnapshot } from "../src/bridge";

describe("Bridge and MockBridge fixtures", () => {
  it("parses scenario query parameters with safe fallback to idle", () => {
    expect(parseScenarioParam("")).toBe("idle");
    expect(parseScenarioParam("?scenario=running")).toBe("running");
    expect(parseScenarioParam("?scenario=failure")).toBe("failure");
    expect(parseScenarioParam("?scenario=empty")).toBe("empty");
    expect(parseScenarioParam("?scenario=unknown")).toBe("idle");
  });

  it("returns scenario-specific snapshots and notifies subscribers on project change", async () => {
    const bridge = new MockBridge("running");
    const snap = await bridge.call<AppSnapshot>("app.snapshot");
    expect(snap.scenario).toBe("running");
    expect(snap.runState).toBe("running");
    expect(snap.activePhaseTitle).toBeTruthy();

    const listener = vi.fn();
    const unsub = bridge.subscribe("app.snapshot", listener);
    await bridge.call("app.selectProject", { projectId: "drivelift-android" });
    expect(listener).toHaveBeenCalledTimes(1);
    const updated = await bridge.call<AppSnapshot>("app.snapshot");
    expect(updated.activeProjectId).toBe("drivelift-android");

    unsub();
    await bridge.call("app.selectProject", { projectId: "nightshift-desktop" });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("selects MockBridge when ?scenario= is provided", async () => {
    const bridge = createBridge("?scenario=empty");
    const snap = await bridge.call<AppSnapshot>("app.snapshot");
    expect(snap.scenario).toBe("empty");
    expect(snap.projects).toHaveLength(0);
  });
});
