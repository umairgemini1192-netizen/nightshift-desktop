import { beforeEach, describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MockBridge } from "../src/bridge/mockBridge";
import { useAppStore } from "../src/app/store";
import { TokenSavingsCard } from "../src/modules/token-optimizer/TokenSavingsCard";
import { TokenOptimizerSettingsCard } from "../src/modules/token-optimizer/SettingsCard";
import { SCENARIO_FIXTURES } from "../src/bridge/fixtures";

describe("Token Optimizer UI components", () => {
  beforeEach(() => {
    const bridge = new MockBridge("running");
    useAppStore.setState({
      bridge,
      snapshot: SCENARIO_FIXTURES.running,
      toasts: [],
    });
  });

  it("renders Mission Control TokenSavingsCard with nightly total, per-tool estimates, and warnings", () => {
    const savings = SCENARIO_FIXTURES.running.deck?.token_savings_tonight;
    expect(savings).toBeDefined();
    render(<TokenSavingsCard savings={savings!} />);

    expect(screen.getByText("Token optimizer savings")).toBeInTheDocument();
    expect(screen.getByText(/18,400 tokens saved tonight \(estimate\)/i)).toBeInTheDocument();
    expect(screen.getByText("repomix")).toBeInTheDocument();
    expect(screen.getByText("rtk-oss")).toBeInTheDocument();
    expect(screen.getByText(/agy-fast stayed disabled/i)).toBeInTheDocument();
  });

  it("renders TokenOptimizerSettingsCard, enforces catalogue/isolation gates, and toggles repomix", async () => {
    render(<TokenOptimizerSettingsCard />);

    await waitFor(() => {
      expect(screen.getByText("Token Optimizer")).toBeInTheDocument();
      expect(screen.getByText("ccusage")).toBeInTheDocument();
    });

    expect(screen.getByText("repomix")).toBeInTheDocument();
    expect(screen.getByText("agy-fast")).toBeInTheDocument();
    expect(screen.getByText("rtk-oss")).toBeInTheDocument();
    expect(screen.getByText("ripgrep (rg.exe)")).toBeInTheDocument();
    expect(screen.getByText("ast-grep-mcp")).toBeInTheDocument();

    // Catalogue-only ast-grep-mcp and isolation-blocked agy-fast toggles are disabled
    const astToggle = screen.getByRole("switch", { name: /Enable ast-grep-mcp/i });
    expect(astToggle).toBeDisabled();
    const agyToggle = screen.getByRole("switch", { name: /Enable agy-fast/i });
    expect(agyToggle).toBeDisabled();

    // Toggling repomix updates state
    const repomixToggle = screen.getByRole("switch", { name: /Enable repomix/i });
    expect(repomixToggle).not.toBeDisabled();
    expect(repomixToggle).toHaveAttribute("aria-checked", "true");
    fireEvent.click(repomixToggle);
    await waitFor(() => {
      expect(repomixToggle).toHaveAttribute("aria-checked", "false");
    });

    // Running isolation probe shows toast / keeps agy-fast disabled
    const probeBtn = screen.getByRole("button", { name: /Run isolation probe/i });
    fireEvent.click(probeBtn);
    await waitFor(() => {
      expect(agyToggle).toBeDisabled();
    });
  });
});
