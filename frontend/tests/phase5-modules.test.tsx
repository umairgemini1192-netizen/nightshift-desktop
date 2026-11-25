import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { useAppStore } from "../src/app/store";
import PlanBuilderPage from "../src/modules/plan-builder/Page";
import RunsHistoryPage from "../src/modules/runs-history/Page";
import EnginesQuotaPage from "../src/modules/engines-quota/Page";
import SettingsPage from "../src/modules/settings/Page";

describe("Phase 5 Core Modules UI", () => {
  it("renders Plan Builder page with phase list, validation, and editor", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <PlanBuilderPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Plan Builder" })).toBeInTheDocument();
    expect(await screen.findByText(/Foundation & Schema Migrations/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Validate/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start run/i })).toBeInTheDocument();

    // Trigger plan validation
    await user.click(screen.getByRole("button", { name: /Validate/i }));
    await waitFor(() => {
      expect(
        screen.getByText(/Plan is valid: DAG is acyclic/i),
      ).toBeInTheDocument();
    });
  });

  it("saves new phases before setting an implementer", async () => {
    const user = userEvent.setup();
    const bridge = useAppStore.getState().bridge;
    const call = vi.spyOn(bridge, "call");
    render(<MemoryRouter><PlanBuilderPage /></MemoryRouter>);
    await screen.findByText(/Foundation & Schema Migrations/i);
    await user.click(screen.getByRole("button", { name: "Add phase" }));
    await user.click(screen.getByRole("checkbox", { name: /All / }));
    await user.click(screen.getByRole("button", { name: "Set implementer" }));
    await waitFor(() => expect(call.mock.calls.some(([method]) => method === "plan.setImplementer")).toBe(true));
    const saveIndex = call.mock.calls.findIndex(([method]) => method === "plan.save");
    const overrideIndex = call.mock.calls.findIndex(([method]) => method === "plan.setImplementer");
    expect(saveIndex).toBeGreaterThanOrEqual(0);
    expect(saveIndex).toBeLessThan(overrideIndex);
    const saved = call.mock.calls[saveIndex][1] as { phases: unknown[] };
    expect(saved.phases.length).toBeGreaterThan(5);
    call.mockRestore();
  });

  it("renders Runs & History page and drills down into run details with morning digest", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <RunsHistoryPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Runs & History" })).toBeInTheDocument();
    expect(await screen.findByText("Nightly Continuous Delivery #14")).toBeInTheDocument();

    // Click "View details" on first run
    const viewButtons = screen.getAllByRole("button", { name: /View details/i });
    expect(viewButtons.length).toBeGreaterThan(0);
    await user.click(viewButtons[0]);

    // Verify drilldown into Morning Digest heading and Per-Phase Timeline
    expect(await screen.findByRole("heading", { name: "Morning Digest" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Per-Phase Timeline" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open artifact folder/i })).toBeInTheDocument();
  });

  it("renders AI Engines & Quota page with EngineCards, ladder editor, and ledger chart", async () => {
    render(
      <MemoryRouter>
        <EnginesQuotaPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "AI Engines & Quota" })).toBeInTheDocument();
    const agyElements = await screen.findAllByText("Antigravity");
    expect(agyElements.length).toBeGreaterThanOrEqual(1);

    expect(screen.getAllByText("Gemini CLI").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Codex CLI").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Claude Code").length).toBeGreaterThanOrEqual(1);

    // Verify Claude pinned last explanation note
    expect(
      screen.getByText(/Claude is pinned last due to token cost/i),
    ).toBeInTheDocument();

    // Verify ledger chart with (estimates) label
    expect(screen.getByText(/Quota & Token Ledger \(estimates\)/i)).toBeInTheDocument();
  });

  it("renders Settings page with projects CRUD, fallback priority, and system toggles", async () => {
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(await screen.findByText("Night-Shift Desktop")).toBeInTheDocument();
    expect(screen.getByText("Engine Fallback Priority")).toBeInTheDocument();
    expect(screen.getByText("Guard & Circuit Breaker Limits")).toBeInTheDocument();
    expect(screen.getByText(/Keep Windows awake while running/i)).toBeInTheDocument();
  });
});
