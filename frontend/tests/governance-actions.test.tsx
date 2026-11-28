import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { useAppStore } from "../src/app/store";
import { MockBridge } from "../src/bridge/mockBridge";
import type { AppSnapshot } from "../src/bridge/types";
import MissionControlPage from "../src/modules/mission-control/Page";
import SettingsPage from "../src/modules/settings/Page";

function seedGovernanceSnapshot(bridge: MockBridge) {
  const base = structuredClone(useAppStore.getState().snapshot);
  const snapshot: AppSnapshot = {
    ...base,
    scenario: "failure",
    runState: "failed",
    headline: "3 phase(s) need your attention",
    activeProjectId: "proj-gov",
    projects: [{ id: "proj-gov", name: "NightShift", repo: "owner/repo" }],
    deck: {
      project_id: "proj-gov",
      project_name: "NightShift",
      state: "attention",
      headline: "3 phase(s) need your attention",
      needs_you: [
        {
          phase_id: "p-gov",
          task_id: "t-gov-1",
          head_sha: "abc1234deadbeef",
          risk_class: "governance",
          pr_number: 42,
          headline: "Governance PR #42 needs your approval before merge",
          detail: "Changes test files and requires explicit owner approval.",
          action: "Review & Approve",
          action_kind: "approve_task",
        },
        {
          phase_id: "release:owner/repo:v1.0.0-preview.2",
          project_id: "proj-gov",
          repo: "owner/repo",
          release_tag: "v1.0.0-preview.2",
          previous_good_tag: "v1.0.0-preview.1",
          headline: "Release v1.0.0-preview.2 failed launch smoke test",
          detail: "App exited with code 1. Previous good release: v1.0.0-preview.1.",
          action: "Roll back release",
          action_kind: "rollback_release",
        },
        {
          phase_id: "p-smoke",
          task_id: "t-smoke-1",
          project_id: "proj-gov",
          headline: "Release smoke test blocked (v1.0.0-preview.3)",
          detail: "No post-release launch/smoke command is configured.",
          action: "Configure smoke test",
          action_kind: "configure_smoke_test",
        },
      ],
      done_tonight: [],
      now: { phase: null, step: null, engine: null, eta: null },
      next: [],
      usage_tonight: {},
      handoff_count: 0,
      updated_at: "2026-11-28T03:00:00Z",
    },
  };
  useAppStore.setState({ bridge, snapshot, toasts: [] });
}

describe("Phase 8e governance UI handlers", () => {
  it("wires Review & Approve through ConfirmDialog to pipeline.approveTask", async () => {
    const user = userEvent.setup();
    const bridge = new MockBridge("failure");
    seedGovernanceSnapshot(bridge);
    const callSpy = vi.spyOn(bridge, "call");

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<MissionControlPage />} />
          <Route path="/settings" element={<div>Settings Route</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Review & Approve" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByText(/abc1234/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve and resume" }));

    await waitFor(() => {
      expect(callSpy).toHaveBeenCalledWith("pipeline.approveTask", {
        taskId: "t-gov-1",
        headSha: "abc1234deadbeef",
        projectId: "proj-gov",
        approvedBy: "owner",
        resume: true,
      });
    });
    callSpy.mockRestore();
  });

  it("wires Roll back release through ConfirmDialog to pipeline.rollbackRelease", async () => {
    const user = userEvent.setup();
    const bridge = new MockBridge("failure");
    seedGovernanceSnapshot(bridge);
    const callSpy = vi.spyOn(bridge, "call");

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<MissionControlPage />} />
          <Route path="/settings" element={<div>Settings Route</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Roll back release" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByText(/v1\.0\.0-preview\.1/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Roll back release" }));

    await waitFor(() => {
      expect(callSpy).toHaveBeenCalledWith("pipeline.rollbackRelease", {
        tag: "v1.0.0-preview.2",
        repo: "owner/repo",
        projectId: "proj-gov",
      });
    });
    callSpy.mockRestore();
  });

  it("navigates to Settings when clicking Configure smoke test", async () => {
    const user = userEvent.setup();
    const bridge = new MockBridge("failure");
    seedGovernanceSnapshot(bridge);

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<MissionControlPage />} />
          <Route path="/settings" element={<div>Settings Route</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Configure smoke test" }));
    expect(await screen.findByText("Settings Route")).toBeInTheDocument();
  });

  it("saves project smoke_command from Settings project modal", async () => {
    const user = userEvent.setup();
    const bridge = new MockBridge("idle");
    useAppStore.setState({ bridge, toasts: [] });
    const callSpy = vi.spyOn(bridge, "call");

    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    );

    const editButtons = await screen.findAllByRole("button", { name: "Edit" });
    await user.click(editButtons[0]);

    const smokeInput = await screen.findByLabelText("Post-release smoke command");
    expect(screen.getByText(/nightshift-desktop-selftest/i)).toBeInTheDocument();
    await user.clear(smokeInput);
    await user.type(smokeInput, "nightshift-desktop-selftest");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(callSpy).toHaveBeenCalledWith(
        "settings.saveProject",
        expect.objectContaining({
          project: expect.objectContaining({
            smoke_command: "nightshift-desktop-selftest",
          }),
        }),
      );
    });
    callSpy.mockRestore();
  });
});
