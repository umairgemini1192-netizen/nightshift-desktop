import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { useAppStore } from "../src/app/store";
import { MockBridge } from "../src/bridge/mockBridge";
import MissionControlPage from "../src/modules/mission-control/Page";
import SettingsPage from "../src/modules/settings/Page";

describe("Phase 6 Packaging, First-Run Import, and Autostart", () => {
  it("offers legacy queue.json import on first run and imports cleanly", async () => {
    const user = userEvent.setup();
    const mockBridge = new MockBridge("empty");
    const callSpy = vi.spyOn(mockBridge, "call");
    const emptySnapshot = await mockBridge.call("app.snapshot");
    useAppStore.setState({
      bridge: mockBridge,
      snapshot: emptySnapshot as ReturnType<typeof useAppStore.getState>["snapshot"],
    });

    render(
      <MemoryRouter>
        <MissionControlPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Welcome to Night-Shift")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Import legacy queue\.json/i }));
    expect(screen.getByRole("dialog", { name: /Import legacy queue\.json/i })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Import queue/i }));

    await waitFor(() => {
      expect(callSpy.mock.calls.some(([method]) => method === "plan.importQueue")).toBe(true);
    });
    expect(useAppStore.getState().snapshot.activeProjectId).toBe("owner/imported-repo");
    callSpy.mockRestore();
  });

  it("allows dismissing first-run offer with Start fresh", async () => {
    const user = userEvent.setup();
    const mockBridge = new MockBridge("empty");
    const callSpy = vi.spyOn(mockBridge, "call");
    const emptySnapshot = await mockBridge.call("app.snapshot");
    useAppStore.setState({
      bridge: mockBridge,
      snapshot: emptySnapshot as ReturnType<typeof useAppStore.getState>["snapshot"],
    });

    render(
      <MemoryRouter>
        <MissionControlPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Welcome to Night-Shift")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Start fresh/i }));

    await waitFor(() => {
      expect(callSpy.mock.calls.some(([method]) => method === "app.dismissFirstRun")).toBe(true);
      expect(screen.queryByText("Welcome to Night-Shift")).not.toBeInTheDocument();
    });
    callSpy.mockRestore();
  });

  it("loads and toggles per-user autostart via Settings bridge", async () => {
    const user = userEvent.setup();
    const mockBridge = new MockBridge("idle");
    const callSpy = vi.spyOn(mockBridge, "call");
    const idleSnapshot = await mockBridge.call("app.snapshot");
    useAppStore.setState({
      bridge: mockBridge,
      snapshot: idleSnapshot as ReturnType<typeof useAppStore.getState>["snapshot"],
    });

    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    const autostartToggle = screen.getByRole("switch", {
      name: /Start Night-Shift on Windows sign-in \(per-user\)/i,
    });
    expect(autostartToggle).toHaveAttribute("aria-checked", "false");

    await user.click(autostartToggle);

    await waitFor(() => {
      expect(
        callSpy.mock.calls.some(
          ([method, args]) => method === "settings.setAutostart" && args?.enabled === true,
        ),
      ).toBe(true);
      expect(autostartToggle).toHaveAttribute("aria-checked", "true");
    });
    callSpy.mockRestore();
  });
});
