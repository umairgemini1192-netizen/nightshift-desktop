import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../src/app/store";
import { MockBridge } from "../src/bridge/mockBridge";
import type { ScenarioName } from "../src/bridge/types";
import MissionControlPage from "../src/modules/mission-control/Page";
import PipelineLedgerPage from "../src/modules/pipeline-ledger/Page";
import { manifest as pipelineManifest } from "../src/modules/pipeline-ledger/manifest";

async function setupScenario(scenario: ScenarioName) {
  const bridge = new MockBridge(scenario);
  const snapshot = await bridge.call<ReturnType<typeof useAppStore.getState>["snapshot"]>("app.snapshot");
  useAppStore.setState({ bridge, snapshot, toasts: [] });
  return bridge;
}

describe("Pipeline Ledger & Scores module (Phase 8b-8f)", () => {
  beforeEach(() => {
    useAppStore.setState({ toasts: [] });
  });

  it("registers as a tools module with manifest metadata", () => {
    expect(pipelineManifest.id).toBe("pipeline-ledger");
    expect(pipelineManifest.section).toBe("tools");
    expect(pipelineManifest.route).toBe("/pipeline-ledger");
  });

  it("renders summary stat tiles, 9-stage pipeline progress, and estimate labels", async () => {
    await setupScenario("running");
    render(
      <MemoryRouter>
        <PipelineLedgerPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Pipeline ledger & scores")).toBeInTheDocument();
    });
    expect(screen.getByText("Accepted changes (30d)")).toBeInTheDocument();
    expect(screen.getByText("Cost per accepted change (estimate)")).toBeInTheDocument();
    expect(screen.getByText("Gold saved (estimate)")).toBeInTheDocument();
    expect(screen.getByText("9-stage pipeline progress")).toBeInTheDocument();

    // Expand Details evidence inspector
    fireEvent.click(screen.getByRole("button", { name: /Details/i }));
    expect(screen.getByText("circleci:desktop-backend")).toBeInTheDocument();
  });

  it("supports Model Scores sorting and graduation badges", async () => {
    await setupScenario("running");
    render(
      <MemoryRouter>
        <PipelineLedgerPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Model Scores/i })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("tab", { name: /Model Scores/i }));

    expect(screen.getByText("qwen3-coder", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Benchmarking (6/10)")).toBeInTheDocument();
    expect(screen.getAllByText("Graduated").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /Sort by acceptance/i }));
    expect(screen.getByText("Cost / accepted (estimate)")).toBeInTheDocument();
  });

  it("handles governance owner approval and release rollback via ConfirmDialog", async () => {
    await setupScenario("failure");
    render(
      <MemoryRouter>
        <PipelineLedgerPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Review & approve/i })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /Review & approve/i }));
    expect(screen.getByText("Approve task for merge")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Approve exact commit/i }));

    await waitFor(() => {
      expect(screen.queryByText("Approve task for merge")).not.toBeInTheDocument();
    });

    // Switch to Maturity & Rollback tab and roll back failed smoke release
    fireEvent.click(screen.getByRole("tab", { name: /Maturity & Rollback/i }));
    expect(screen.getAllByText("Integration-tested").length).toBe(9);
    fireEvent.click(screen.getByRole("button", { name: /Roll back/i }));
    expect(screen.getByRole("heading", { name: "Roll back release" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Roll back release/i }));

    await waitFor(() => {
      expect(screen.getByText("Smoke: rolled_back")).toBeInTheDocument();
    });
  });

  it("renders empty state when no pipeline tasks exist", async () => {
    await setupScenario("empty");
    render(
      <MemoryRouter>
        <PipelineLedgerPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("No pipeline tasks recorded yet")).toBeInTheDocument();
    });
  });

  it("integrates pipeline stage badge, free-only spend, and ledger link into Mission Control", async () => {
    await setupScenario("running");
    render(
      <MemoryRouter>
        <MissionControlPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("$0.00 / $0.00 (free-only mode)")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Pipeline ledger/i })).toBeInTheDocument();
  });
});
