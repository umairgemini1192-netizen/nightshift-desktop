import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { useAppStore } from "../src/app/store";
import { MockBridge } from "../src/bridge/mockBridge";
import MissionControlPage from "../src/modules/mission-control/Page";
import PipelineLedgerPage from "../src/modules/pipeline-ledger/Page";

function mountPipelineLedger(scenario: "running" | "empty" | "failure" = "running") {
  const bridge = new MockBridge(scenario);
  const callSpy = vi.spyOn(bridge, "call");
  useAppStore.setState({
    bridge,
    snapshot: {
      ...useAppStore.getState().snapshot,
      scenario,
      activeProjectId: "nightshift-desktop",
    },
    toasts: [],
  });
  const utils = render(
    <MemoryRouter initialEntries={["/pipeline-ledger"]}>
      <Routes>
        <Route path="/pipeline-ledger" element={<PipelineLedgerPage />} />
        <Route path="/" element={<MissionControlPage />} />
      </Routes>
    </MemoryRouter>,
  );
  return { ...utils, bridge, callSpy };
}

describe("Pipeline Ledger & Scores module (Phase 8f)", () => {
  it("renders KPI tiles with estimate labels, 9-stage pipeline strip, risk badges, and maturity labels", async () => {
    mountPipelineLedger("running");

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /Pipeline ledger & scores/i })).toBeInTheDocument();
    });

    // Estimate labels on cost & gold-saved KPIs
    expect(screen.getByText(/Cost per accepted change \(estimate\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Gold saved \(estimate\)/i)).toBeInTheDocument();
    expect(screen.getByText("$0.14")).toBeInTheDocument();
    expect(screen.getByText("9 tasks")).toBeInTheDocument();

    // Active pipeline 9-stage progress bar + risk & maturity badges
    const progressBar = screen.getByRole("progressbar", { name: /9-stage pipeline for p8f-t01/i });
    expect(progressBar).toHaveAttribute("aria-valuemax", "9");
    expect(screen.getByText("Logic")).toBeInTheDocument();
    expect(screen.getByText("Integration-tested")).toBeInTheDocument();

    // Expand evidence inspector behind Details
    await userEvent.click(screen.getByRole("button", { name: /^Details$/i }));
    expect(screen.getByText(/Verified evidence & artifacts/i)).toBeInTheDocument();
    expect(screen.getByText("circleci:desktop-backend")).toBeInTheDocument();
  });

  it("sorts Model Scores table and displays graduation badges and estimate suffixes", async () => {
    mountPipelineLedger("running");
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Model Scores/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("tab", { name: /Model Scores/i }));

    expect(screen.getByText("Benchmarking (6/10)")).toBeInTheDocument();
    expect(screen.getByText("Demoted")).toBeInTheDocument();
    expect(screen.getAllByText(/\(estimate\)/i).length).toBeGreaterThan(0);

    // Sort by sample tasks descending
    const sortBtn = screen.getByRole("button", { name: /Sample tasks/i });
    await user.click(sortBtn);
    const rows = screen.getAllByRole("row");
    expect(within(rows[1]).getByText("qwen")).toBeInTheDocument();
  });

  it("filters Task Ledger by risk class and expands StepFeed", async () => {
    mountPipelineLedger("running");
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Task Ledger/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("tab", { name: /Task Ledger/i }));

    expect(screen.getByText(/Triage role on small_job_ladder/i)).toBeInTheDocument();
    expect(screen.getByText(/Risk classifier, independent reviewer/i)).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: /Filter by risk class/i }));
    await user.click(screen.getByRole("option", { name: "High risk" }));
    expect(screen.queryByText(/Triage role on small_job_ladder/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Risk classifier, independent reviewer/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Steps$/i }));
    expect(screen.getByText(/REVIEW \(reviewer\) via codex/i)).toBeInTheDocument();
  });

  it("runs one-click Roll back ConfirmDialog in Maturity & Rollback tab", async () => {
    const { callSpy } = mountPipelineLedger("running");
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Maturity & Rollback/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("tab", { name: /Maturity & Rollback/i }));

    expect(screen.getByText(/Pipeline stage maturity \(Amendment J\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Monthly budget/i)).toBeInTheDocument();
    expect(screen.getByText("v1.0.0-preview.2")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Roll back$/i }));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByRole("heading", { name: /Roll back release/i })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /Roll back release/i }));

    await waitFor(() => {
      expect(callSpy).toHaveBeenCalledWith(
        "pipeline.rollbackRelease",
        expect.objectContaining({ tag: "v1.0.0-preview.2" }),
      );
    });
  });

  it("renders empty state and error state with Retry button", async () => {
    const emptyRun = mountPipelineLedger("empty");
    await waitFor(() => {
      expect(screen.getByText(/No pipeline tasks recorded yet/i)).toBeInTheDocument();
    });
    emptyRun.unmount();

    const errRun = mountPipelineLedger("failure");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /Could not load pipeline ledger/i })).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
    errRun.unmount();
  });
});
