import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { MockBridge } from "../src/bridge/mockBridge";
import { useAppStore } from "../src/app/store";
import PlanStudioPage from "../src/modules/plan-studio/Page";

describe("Plan Studio & AI Planning Council UI", () => {
  it("renders goal textarea, project selector, and 3 role cards", async () => {
    render(
      <MemoryRouter>
        <PlanStudioPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Plan Studio" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Implementation goal/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Target project/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Convene Planning Council/i })).toBeInTheDocument();

    // Verify 3 council role cards are rendered
    await waitFor(() => {
      expect(screen.getAllByText(/Gemini CLI/i).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/Codex CLI/i).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/Claude Code/i).length).toBeGreaterThan(0);
    });
  });

  it("handles substitution notice when Gemini is cooling down", async () => {
    useAppStore.setState({
      bridge: new MockBridge("council-substitution"),
    });

    render(
      <MemoryRouter>
        <PlanStudioPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText(/Gemini cooling down until 03:45; Codex substituted/i)).toBeInTheDocument();
    });
  });

  it("shows diff trade-off summary and approves plan sending to Plan Builder", async () => {
    const user = userEvent.setup();
    const bridge = new MockBridge("council-final-ready");
    const callSpy = vi.spyOn(bridge, "call");
    useAppStore.setState({ bridge });

    render(
      <MemoryRouter>
        <PlanStudioPage />
      </MemoryRouter>,
    );

    // Verify trade-off summary and Draft -> Final diff with per-AI notes
    await waitFor(() => {
      expect(screen.getByText(/Council Trade-off Analysis:/i)).toBeInTheDocument();
      expect(screen.getByText(/Plan diff \(Draft to Final\) and per-AI notes/i)).toBeInTheDocument();
      expect(screen.getByText(/Added in critique/i)).toBeInTheDocument();
      expect(screen.getAllByText(/conflict resolution/i).length).toBeGreaterThan(0);
    });

    // Inline edit first phase title
    const titleInput = screen.getByLabelText("Phase title", { selector: "#phase-title-p1" });
    await user.clear(titleInput);
    await user.type(titleInput, "Updated Cache Schema");
    expect(titleInput).toHaveValue("Updated Cache Schema");

    // Click Approve
    const approveBtn = screen.getByRole("button", { name: /Approve and send to Plan Builder/i });
    expect(approveBtn).toBeInTheDocument();
    await user.click(approveBtn);

    await waitFor(() => {
      expect(callSpy.mock.calls.some(([method]) => method === "council.approve")).toBe(true);
      expect(callSpy.mock.calls.some(([method]) => method === "plan.save")).toBe(true);
    });

    callSpy.mockRestore();
  });
});
