import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { RouteErrorBoundary } from "../src/app/RouteErrorBoundary";
import { useAppStore } from "../src/app/store";
import { SCENARIO_FIXTURES } from "../src/bridge/fixtures";
import { MockBridge } from "../src/bridge/mockBridge";
import EnginesQuotaPage from "../src/modules/engines-quota/Page";
import MissionControlPage from "../src/modules/mission-control/Page";
import PlanBuilderPage from "../src/modules/plan-builder/Page";
import RunsHistoryPage from "../src/modules/runs-history/Page";
import { TokenSavingsCard } from "../src/modules/token-optimizer/TokenSavingsCard";

describe("S1a UI truth & resilience fixes", () => {
  it("renders AI Engines & Quota with object-shaped ladders (runner:model) and empty ledger state", async () => {
    const bridge = new MockBridge("idle");
    const origCall = bridge.call.bind(bridge);
    vi.spyOn(bridge, "call").mockImplementation(async (method, args) => {
      if (method === "engines.getLadders") {
        return {
          ladders: {
            backend: [
              { runner: "agy", model: "gemini-4-neon", effort: "medium" },
              { runner: "gemini", model: "gemini-2.5-pro", effort: "medium" },
              { runner: "claude", model: "claude-3-7-sonnet", effort: "medium" },
            ],
          },
          pinnedNote: "Claude is pinned last due to token cost.",
        } as never;
      }
      if (method === "engines.getLedger") {
        return { routerDecisions: [], chartData: [], isEstimate: true } as never;
      }
      return origCall(method, args);
    });
    useAppStore.setState({ bridge });

    render(
      <MemoryRouter>
        <EnginesQuotaPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText(/agy:gemini-4-neon/i)).toBeInTheDocument();
    expect(screen.getByText(/gemini:gemini-2\.5-pro/i)).toBeInTheDocument();
    expect(screen.getByText("No data yet")).toBeInTheDocument();
  });

  it("isolates route crashes with RouteErrorBoundary, logs error, and recovers on Reload", async () => {
    const user = userEvent.setup();
    const onError = vi.fn();
    let shouldCrash = true;

    function CrashablePage() {
      if (shouldCrash) {
        throw new Error("Simulated page crash");
      }
      return <h1>Recovered Page</h1>;
    }

    render(
      <MemoryRouter initialEntries={["/crash"]}>
        <Routes>
          <Route
            path="/crash"
            element={
              <RouteErrorBoundary routeKey="/crash" routeTitle="Engines" onError={onError}>
                <CrashablePage />
              </RouteErrorBoundary>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Unable to render Engines")).toBeInTheDocument();
    expect(screen.getByText("Simulated page crash")).toBeInTheDocument();
    expect(onError).toHaveBeenCalledTimes(1);

    shouldCrash = false;
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(screen.getByRole("heading", { name: "Recovered Page" })).toBeInTheDocument();
  });

  it("shows explicit 'No data yet' empty state on Mission Control and TokenSavingsCard when no real data exists", async () => {
    const bridge = new MockBridge("idle");
    const emptyUsageSnapshot = {
      ...SCENARIO_FIXTURES.idle,
      deck: {
        ...SCENARIO_FIXTURES.idle.deck!,
        usage_tonight: {},
        handoff_count: 0,
        budget_tonight: {
          freeOnlyMode: true,
          spentUsdEst: 0,
          capUsdEst: 0,
          invocations: 0,
          maxInvocations: 20,
          wallHours: 0,
          maxHours: 8,
          displayLine: "$0.00 / $0.00 (free-only mode)",
          isEstimate: true,
        },
        gold_saved_tonight: {
          jobs_done: 0,
          cost_saved_usd: 0,
          tokens_saved: 0,
          estimate_label: "$0.00 gold saved tonight (estimate)",
        },
        token_savings_tonight: {
          totalTokensSaved: 0,
          estimateLabel: "~0 tokens saved tonight (estimate)",
          perTool: [],
          warnings: [],
          isEstimate: true,
        },
      },
    };
    useAppStore.setState({ bridge, snapshot: emptyUsageSnapshot });

    render(
      <MemoryRouter>
        <MissionControlPage />
      </MemoryRouter>,
    );

    const noDataStates = await screen.findAllByText("No data yet");
    expect(noDataStates.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/18,400/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$0\.90/)).not.toBeInTheDocument();

    render(
      <TokenSavingsCard
        savings={{
          totalTokensSaved: 0,
          estimateLabel: "~0 tokens saved tonight (estimate)",
          perTool: [],
          warnings: [],
          isEstimate: true,
        }}
      />,
    );
    expect(screen.getAllByText("No data yet").length).toBeGreaterThanOrEqual(3);
  });

  it("shows '-' instead of fabricated duration/date/engines on Runs & History", async () => {
    const bridge = new MockBridge("idle");
    vi.spyOn(bridge, "call").mockImplementation(async (method) => {
      if (method === "runs.list") {
        return [
          {
            id: "run_real_1",
            projectId: "proj1",
            title: "Real Run 1",
            status: "completed",
            phasesCount: 2,
            phasesDone: 2,
            phasesFailed: 0,
            duration: "",
            aiUsed: [],
            createdAt: "",
            updatedAt: "",
          },
        ] as never;
      }
      return [] as never;
    });
    useAppStore.setState({ bridge });

    render(
      <MemoryRouter>
        <RunsHistoryPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Real Run 1")).toBeInTheDocument();
    expect(screen.queryByText("1 h 42 m")).not.toBeInTheDocument();
    expect(screen.queryByText("Nov 25, 22:15")).not.toBeInTheDocument();
    expect(screen.getAllByText("-").length).toBeGreaterThanOrEqual(3);
  });

  it("shows and allows imported phase ladder value (e.g. 'build') in Plan Builder", async () => {
    const bridge = new MockBridge("idle");
    const origCall = bridge.call.bind(bridge);
    vi.spyOn(bridge, "call").mockImplementation(async (method, args) => {
      if (method === "plan.get") {
        return {
          plan: { id: "plan_imp", title: "Imported Plan", project_id: "proj1" },
          phases: [
            {
              id: "p1",
              title: "Build APK",
              branch: "feat/build",
              prompt: "Build it",
              depends_on: [],
              ladder: "build",
              timeout_minutes: 60,
              auto_merge: true,
              release_after_merge: false,
            },
          ],
        } as never;
      }
      if (method === "engines.getLadders") {
        return { ladders: { build: ["agy", "gemini", "claude"] }, pinnedNote: "" } as never;
      }
      return origCall(method, args);
    });
    useAppStore.setState({ bridge });

    render(
      <MemoryRouter>
        <PlanBuilderPage />
      </MemoryRouter>,
    );

    const select = await screen.findByLabelText("Failover ladder");
    await waitFor(() => {
      expect(select).toHaveTextContent("build");
    });
    expect(screen.queryByText("Choose an option")).not.toBeInTheDocument();
  });

  it("shows 'Disabled by owner' without Fix button and never shows Fix button for Gemini CLI", async () => {
    const bridge = new MockBridge("idle");
    useAppStore.setState({
      bridge,
      snapshot: {
        ...SCENARIO_FIXTURES.idle,
        logins: {
          ...SCENARIO_FIXTURES.idle.logins,
          claude: { tool: "claude", state: "Disabled by owner", detail: "claude is disabled by owner" },
          gemini: { tool: "gemini", state: "Logged out", detail: "gemini API key missing" },
        },
      },
    });

    render(
      <MemoryRouter>
        <MissionControlPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Disabled by owner")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fix" })).not.toBeInTheDocument();
  });
});
