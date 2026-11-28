import { describe, expect, it, vi } from "vitest";
import {
  createBridge,
  formatEventPlainText,
  isNotifiableEvent,
  mapEventToNotification,
  MockBridge,
  parseScenarioParam,
  REALISTIC_STREAMS,
  SCENARIO_FIXTURES,
  type AppSnapshot,
} from "../src/bridge";

describe("Bridge and MockBridge fixtures", () => {
  it("parses scenario query parameters with safe fallback to idle", () => {
    expect(parseScenarioParam("")).toBe("idle");
    expect(parseScenarioParam("?scenario=running")).toBe("running");
    expect(parseScenarioParam("?scenario=failure")).toBe("failure");
    expect(parseScenarioParam("?scenario=empty")).toBe("empty");
    expect(parseScenarioParam("?scenario=gemini-logged-out")).toBe("gemini-logged-out");
    expect(parseScenarioParam("?scenario=claude-limit")).toBe("claude-limit");
    expect(parseScenarioParam("?scenario=gh-logged-out")).toBe("gh-logged-out");
    expect(parseScenarioParam("?scenario=handoff")).toBe("handoff");
    expect(parseScenarioParam("?scenario=ci-healing")).toBe("ci-healing");
    expect(parseScenarioParam("?scenario=finished")).toBe("finished");
    expect(parseScenarioParam("?scenario=governance")).toBe("governance");
    expect(parseScenarioParam("?scenario=unknown")).toBe("idle");
  });

  it("selects MockBridge when ?scenario= is provided", async () => {
    const bridge = createBridge("?scenario=empty");
    const snap = await bridge.call<AppSnapshot>("app.snapshot");
    expect(snap.scenario).toBe("empty");
    expect(snap.projects).toHaveLength(0);
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

  it("provides rich fixtures for all required login and execution scenarios", () => {
    // 1. All logins ready
    const idleSnap = SCENARIO_FIXTURES.idle;
    expect(idleSnap.logins?.git.state).toBe("Ready");
    expect(idleSnap.logins?.gh.state).toBe("Ready");
    expect(idleSnap.logins?.gemini.state).toBe("Ready");
    expect(idleSnap.logins?.claude.state).toBe("Ready");

    // 2. Gemini logged out
    const geminiSnap = SCENARIO_FIXTURES["gemini-logged-out"];
    expect(geminiSnap.logins?.gemini.state).toBe("Logged out");
    expect(geminiSnap.logins?.gemini.detail).toContain("gemini");

    // 3. Claude limit reached with back-at time
    const claudeSnap = SCENARIO_FIXTURES["claude-limit"];
    expect(claudeSnap.logins?.claude.state).toBe("Limit reached");
    expect(claudeSnap.logins?.claude.backAt).toBe("03:10");

    // 4. GitHub CLI logged out pausing the run
    const ghSnap = SCENARIO_FIXTURES["gh-logged-out"];
    expect(ghSnap.logins?.gh.state).toBe("Logged out");
    expect(ghSnap.runState).toBe("failed");
    expect(ghSnap.deck?.needs_you[0]?.headline).toContain("GitHub CLI");

    // 5. Handoff scenario
    const handoffSnap = SCENARIO_FIXTURES.handoff;
    expect(handoffSnap.deck?.now.step).toContain("checkpoint");
    expect(handoffSnap.deck?.handoff_count).toBeGreaterThan(0);

    // 6. CI failure self-healing
    const ciSnap = SCENARIO_FIXTURES["ci-healing"];
    expect(ciSnap.deck?.now.step).toContain("Codex fixing");

    // 7. Parked phase with stacked children
    const failureSnap = SCENARIO_FIXTURES.failure;
    expect(failureSnap.deck?.needs_you[0]?.headline).toContain("Phase 2 is parked");
    expect(failureSnap.deck?.now.step).toContain("stacked");

    // 8. Finished run
    const finishedSnap = SCENARIO_FIXTURES.finished;
    expect(finishedSnap.runState).toBe("done");
    expect(finishedSnap.deck?.done_tonight.length).toBe(5);

    // 9. Governance approval, release rollback, and smoke-config actions
    const governanceSnap = SCENARIO_FIXTURES.governance;
    expect(governanceSnap.runState).toBe("failed");
    expect(governanceSnap.deck?.needs_you.map((item) => item.action_kind)).toEqual([
      "approve_task",
      "rollback_release",
      "configure_smoke_test",
    ]);
  });

  it("handles engine stop, undoStop, and start RPC methods", async () => {
    const bridge = new MockBridge("running");
    const stopRes = await bridge.call<{ armed: boolean }>("engine.stopAfterPhase");
    expect(stopRes.armed).toBe(true);

    const snapArmed = await bridge.call<AppSnapshot>("app.snapshot");
    expect(snapArmed.stopArmed).toBe(true);

    const undoRes = await bridge.call<{ armed: boolean }>("engine.undoStop");
    expect(undoRes.armed).toBe(false);

    const snapDisarmed = await bridge.call<AppSnapshot>("app.snapshot");
    expect(snapDisarmed.stopArmed).toBe(false);

    const bridgeIdle = new MockBridge("idle");
    const startRes = await bridgeIdle.call<{ started: boolean }>("engine.start");
    expect(startRes.started).toBe(true);
    const snapRunning = await bridgeIdle.call<AppSnapshot>("app.snapshot");
    expect(snapRunning.runState).toBe("running");
  });

  it("replays realistic event streams on subscriber listeners", () => {
    const bridge = new MockBridge("idle");
    const received: string[] = [];
    bridge.subscribe("*", (payload: unknown) => {
      const evt = payload as { kind: string };
      if (evt?.kind) received.push(evt.kind);
    });

    expect(REALISTIC_STREAMS["all-good"]).toBeDefined();
    bridge.playStream("all-good");
    expect(received).toContain("phase.started");
    expect(received).toContain("runner.started");
    expect(received).toContain("ci.passed");
    expect(received).toContain("phase.merged");
    expect(received).toContain("run.finished");

    received.length = 0;
    bridge.playStream("handoff");
    expect(received).toContain("runner.exhausted");
    expect(received).toContain("handoff.started");
    expect(received).toContain("switchback.armed");
  });

  it("maps events to plain-English text without raw error traces", () => {
    expect(
      formatEventPlainText({
        kind: "handoff.started",
        payload: { from_engine: "Claude Code", to_engine: "Antigravity" },
      }),
    ).toBe("Claude Code hit limit; Antigravity continuing from checkpoint.");

    expect(
      formatEventPlainText({
        kind: "ci.failed",
      }),
    ).toBe("Continuous integration checks failed; starting automatic fix round.");

    expect(
      formatEventPlainText({
        kind: "phase.parked",
        payload: { reason: "login change failed review twice" },
      }),
    ).toBe("Phase parked: login change failed review twice.");

    expect(
      formatEventPlainText({
        kind: "circuit_breaker.tripped",
      }),
    ).toBe("Circuit breaker tripped after 3 consecutive parked phases.");

    // Respects plain_text if provided
    expect(
      formatEventPlainText({
        kind: "anything",
        plain_text: "Custom plain language message.",
      }),
    ).toBe("Custom plain language message.");
  });

  it("enforces toast notification rules: only run finished, phase parked, circuit breaker, and power/network loss > 10 min", () => {
    // Allowed: 1. run finished
    expect(isNotifiableEvent({ kind: "run.finished" })).toBe(true);
    expect(mapEventToNotification({ kind: "run.finished" })?.tone).toBe("success");

    // Allowed: 2. phase parked
    expect(isNotifiableEvent({ kind: "phase.parked" })).toBe(true);
    expect(mapEventToNotification({ kind: "phase.parked" })?.tone).toBe("danger");

    // Allowed: 3. circuit breaker tripped
    expect(isNotifiableEvent({ kind: "circuit_breaker.tripped" })).toBe(true);
    expect(mapEventToNotification({ kind: "circuit_breaker.tripped" })?.tone).toBe("warning");

    // Allowed: 4. power/network loss > 10 minutes (> 600s)
    expect(
      isNotifiableEvent({
        kind: "network.lost",
        payload: { duration_seconds: 601 },
      }),
    ).toBe(true);
    expect(
      isNotifiableEvent({
        kind: "power_network_lost",
        payload: { minutes: 15 },
      }),
    ).toBe(true);
    expect(
      mapEventToNotification({
        kind: "network.lost",
        payload: { duration_seconds: 700 },
      })?.tone,
    ).toBe("danger");

    // Disallowed: network loss < 10 minutes
    expect(
      isNotifiableEvent({
        kind: "network.lost",
        payload: { duration_seconds: 120 },
      }),
    ).toBe(false);

    // Disallowed: routine progress events must NEVER trigger toast notifications
    expect(isNotifiableEvent({ kind: "phase.started" })).toBe(false);
    expect(isNotifiableEvent({ kind: "step.started" })).toBe(false);
    expect(isNotifiableEvent({ kind: "runner.started" })).toBe(false);
    expect(isNotifiableEvent({ kind: "ci.passed" })).toBe(false);
    expect(isNotifiableEvent({ kind: "ci.fixing" })).toBe(false);
    expect(isNotifiableEvent({ kind: "checkpoint.created" })).toBe(false);
    expect(isNotifiableEvent({ kind: "logins.checked" })).toBe(false);

    expect(mapEventToNotification({ kind: "phase.started" })).toBeNull();
  });
});
